"""求解层测试：守恒、容量、极值与不可解释时的行为。"""

import _bootstrap  # noqa: F401
import unittest

from footfall.data import Incident, Policy
from footfall.evidence import RestartProof, build_evidence
from footfall.lp import Infeasible, Problem
from footfall.model import EdgeClosure, EntryCap
from footfall.solve import solve

from support import make_dataset


class SimplexTest(unittest.TestCase):
    def test_maximum_and_minimum(self):
        problem = Problem(2)
        problem.le([1, 0], 4)
        problem.le([0, 2], 12)
        problem.le([3, 2], 18)
        # (x, y) = (2, 6)：3·2 + 2·6 = 18，目标 3·2 + 5·6 = 36。
        self.assertAlmostEqual(problem.maximize([3, 5]).value, 36.0)
        self.assertAlmostEqual(problem.minimize([1, 1]).value, 0.0)

    def test_infeasible_detected(self):
        problem = Problem(1)
        problem.ge([1], 5)
        problem.le([1], 2)
        self.assertFalse(problem.feasible())

    def test_equalities_and_negative_rhs(self):
        problem = Problem(2)
        problem.eq([1, 1], 10)
        problem.eq([1, -1], 2)
        solution = problem.maximize([1, 2])
        self.assertAlmostEqual(solution.value, 14.0)


class ConservationTest(unittest.TestCase):
    def test_cross_flow_moves_between_zones_without_creating_people(self):
        # 入口门限为 0：没有外部进出，只有连廊搬运，全馆人数守恒。
        ds = make_dataset(
            zones=[("lobby", 40, 0, 4), ("hall", 20, 0, 2)],
            readings=[],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds)
        self.assertFalse(result.infeasible)
        for window in result.windows():
            total = result.total_range(window).span
            self.assertEqual(total.lo, 6)  # 4 + 2，全馆人数不会凭空增长
            self.assertEqual(total.hi, 6)

    def test_cross_reading_bounds_both_directions_of_edge(self):
        ds = make_dataset(
            readings=[
                ("c", "lobby", "w0", 3, 0, "cross"),
                ("c", "lobby", "w1", 8, 0, "cross"),
            ]
        )
        evidence = build_evidence(ds)
        self.assertEqual(evidence.channel("lobby", "w1", "cross").span.lo, 5)
        result = solve(ds, evidence=evidence)
        self.assertFalse(result.infeasible)

    def test_edge_capacity_limits_crossing(self):
        # 连廊双向合计上限 4，但证据要求 5 人穿过：没有相容解释。
        ds = make_dataset(
            edges=[("e1", "lobby", "hall", 4)],
            readings=[
                ("c", "lobby", "w0", 3, 0, "cross"),
                ("c", "lobby", "w1", 8, 0, "cross"),
            ],
        )
        result = solve(ds, evidence=build_evidence(ds))
        self.assertTrue(result.infeasible)
        self.assertTrue(result.conflicts)

    def test_zone_capacity_and_total_capacity(self):
        ds = make_dataset(
            zones=[("lobby", 30, 20, 10), ("hall", 10, 10, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
        )
        result = solve(ds)
        self.assertFalse(result.infeasible)
        self.assertEqual(result.total_capacity(), 40)
        for zone in ("lobby", "hall"):
            cell = result.zone_range(zone, result.windows()[0])
            self.assertLessEqual(cell.span.hi, result.dataset.topology.zones[zone].capacity)


class ManualCountTest(unittest.TestCase):
    def test_confirmed_manual_count_pins_occupancy(self):
        ds = make_dataset(
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            incidents=[("w1", "lobby", 7, (), True)],
        )
        result = solve(ds)
        self.assertFalse(result.infeasible)
        cell = result.zone_range("lobby", "w1")
        self.assertEqual(cell.span, cell.span.__class__(7, 7))

    def test_unconfirmed_manual_count_is_not_a_hard_constraint(self):
        ds = make_dataset(
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            incidents=[("w1", "lobby", 7, (), False)],
        )
        result = solve(ds)
        cell = result.zone_range("lobby", "w1")
        self.assertNotEqual(cell.span.lo, 7)

    def test_impossible_manual_count_reports_no_explanation(self):
        ds = make_dataset(
            zones=[("lobby", 10, 10, 0), ("hall", 10, 10, 0)],
            incidents=[("w0", "lobby", 11, (), True)],
        )
        result = solve(ds)
        self.assertTrue(result.infeasible)
        self.assertTrue(result.conflicts)


class ClosureTest(unittest.TestCase):
    def test_confirmed_closure_blocks_edge_and_flags_conflicting_reading(self):
        # e2 从 w1 起关闭；w2 却出现只能来自 e2 的 cross 读数。
        ds = make_dataset(
            zones=[("lobby", 30, 20, 4), ("hall", 20, 10, 2)],
            edges=[("e1", "lobby", "hall", 10), ("e2", "lobby", "hall", 10)],
            readings=[
                ("c", "lobby", "w0", 1, 0, "cross"),
                ("c", "lobby", "w2", 6, 0, "cross"),
            ],
            incidents=[("w1", "lobby", None, ("e1", "e2"), True)],
        )
        evidence = build_evidence(ds)
        self.assertEqual(len(evidence.conflicts), 1)
        self.assertIn("关闭通道", evidence.conflict_messages()[0])
        # 冲突读数被撤下，而不是被当成放宽限制的证据。
        channel = evidence.channel("lobby", "w2", "cross")
        self.assertIsNone(channel.span.hi)

    def test_closure_keeps_restriction_and_analysis_proceeds(self):
        ds = make_dataset(
            edges=[("e1", "lobby", "hall", 10), ("e2", "lobby", "hall", 10)],
            readings=[
                ("c", "lobby", "w0", 1, 0, "cross"),
                ("c", "lobby", "w2", 6, 0, "cross"),
            ],
            incidents=[("w1", "lobby", None, ("e1", "e2"), True)],
        )
        evidence = build_evidence(ds)
        result = solve(ds, evidence=evidence)
        self.assertFalse(result.infeasible)
        # 关闭仍然生效：w2 的跨区流量必须为 0。
        model = result.model
        value = model.extreme(
            {
                model.flow_var("e1", "w2", 1): 1.0,
                model.flow_var("e1", "w2", -1): 1.0,
                model.flow_var("e2", "w2", 1): 1.0,
                model.flow_var("e2", "w2", -1): 1.0,
            },
            maximize=True,
        )[0]
        self.assertEqual(value, 0)

    def test_conflict_policy_stop_marks_stop_but_keeps_restriction(self):
        ds = make_dataset(
            edges=[("e1", "lobby", "hall", 10)],
            readings=[
                ("c", "lobby", "w0", 1, 0, "cross"),
                ("c", "lobby", "w1", 6, 0, "cross"),
            ],
            incidents=[("w1", "lobby", None, ("e1",), True)],
            policy=Policy(conflict_policy="stop"),
        )
        evidence = build_evidence(ds)
        self.assertTrue(any("停止" in warning for warning in evidence.warnings))
        self.assertEqual(len(evidence.conflicts), 1)


class EntryCapTest(unittest.TestCase):
    def test_entry_cap_lowers_worst_case_total(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
        )
        base = solve(ds)
        worst = base.total_range("w0").span.hi
        capped = solve(ds, entry_caps=(EntryCap("lobby", "w0", 5),))
        self.assertLess(capped.total_range("w0").span.hi, worst)

    def test_closure_object_removes_capacity(self):
        ds = make_dataset(
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds, closures=(EdgeClosure("e1", "w0"),))
        self.assertFalse(result.infeasible)
        model = result.model
        crossing = model.extreme(
            {
                model.flow_var("e1", "w0", 1): 1.0,
                model.flow_var("e1", "w0", -1): 1.0,
            },
            maximize=True,
        )[0]
        self.assertEqual(crossing, 0)


class ConditionalExtremeTest(unittest.TestCase):
    def test_zone_upper_bound_may_not_be_simultaneously_reachable(self):
        # 两个分区的各自上界由不同解释达到；全馆取上界时某区上界更小。
        ds = make_dataset(
            zones=[
                ("lobby", 50, 50, 50),
                ("hall", 50, 50, 0),
            ],
            readings=[],
        )
        result = solve(ds)
        self.assertFalse(result.infeasible)
        cell = result.zone_range("lobby", "w0")
        self.assertIsNotNone(cell.conditional_total_max)
        self.assertGreaterEqual(cell.span.hi, cell.conditional_total_max.hi)


if __name__ == "__main__":
    unittest.main()
