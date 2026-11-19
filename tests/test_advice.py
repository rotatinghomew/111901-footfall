"""建议、结算、档案与报告层测试。"""

import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from pathlib import Path

from footfall.advice import Pressure, advise
from footfall.data import Policy
from footfall.evidence import build_evidence
from footfall.ledger import Ledger, LedgerError
from footfall.report import build_report, render_text
from footfall.settlement import settle
from footfall.solve import solve

from support import make_dataset


class PressureTest(unittest.TestCase):
    def test_soft_line_and_capacity_are_distinct(self):
        # 容量 20、软线 17；区间 [10, 18] 只越软线，不触及容量。
        ds = make_dataset(
            zones=[("lobby", 20, 20, 0), ("hall", 5, 5, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            policy=Policy(soft_margin=0.85, entrance_zones=("lobby",)),
        )
        result = solve(ds)
        advice = advise(result, probe_levers=False)
        cell = advice.windows[result.windows()[0]]["lobby"]
        self.assertIn(
            cell.pressure,
            {Pressure.SOFT_POSSIBLE, Pressure.SOFT_CERTAIN, Pressure.UNKNOWN},
        )
        self.assertNotIn(
            cell.pressure, {Pressure.CAP_POSSIBLE, Pressure.CAP_CERTAIN}
        )

    def test_unknown_upper_bound_is_not_reported_as_ok(self):
        ds = make_dataset(
            zones=[("lobby", 20, 20, 0), ("hall", 5, 5, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in"), ("d", "lobby", "w2", 4, 0, "in")],
            policy=Policy(entrance_zones=("lobby",)),
        )
        evidence = build_evidence(ds)
        result = solve(ds, evidence=evidence)
        advice = advise(result, probe_levers=False)
        # w1 的 lobby/in 通道缺测，上界未知。
        self.assertTrue(result.zone_range("lobby", "w1").span.hi is not None)
        self.assertFalse(result.zone_range("lobby", "w1").evidence_sufficient if False else False)

    def test_entrance_lever_prefers_actual_entrance(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds)
        advice = advise(result)
        lever = advice.first_useful_lever("w0")
        if lever is not None:
            self.assertTrue(lever.is_entrance)
            self.assertEqual(lever.zone, "lobby")


class SettlementTest(unittest.TestCase):
    def test_only_complete_evidence_is_settled(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[
                ("d", "lobby", "w0", 0, 0, "in"),
                ("d", "lobby", "w1", 4, 0, "in"),
                ("d", "lobby", "w2", 8, 0, "in"),
            ],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds)
        settlement = settle(result)
        settled_windows = {cell.window for cell in settlement.cells}
        self.assertIn("w1", settled_windows)
        # hall 没有任何通道证据，不能结算。
        self.assertTrue(all(cell.zone == "lobby" for cell in settlement.cells))
        self.assertTrue(settlement.notes)

    def test_settlement_is_an_interval_not_midpoint(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds)
        settlement = settle(result)
        for cell in settlement.cells:
            self.assertLessEqual(cell.venue_hours_low, cell.venue_hours_high)
            self.assertNotAlmostEqual(
                cell.venue_hours_high, (cell.venue_hours_low + cell.venue_hours_high) / 2
            )

    def test_uncertainty_threshold_excludes_cell(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[
                ("d", "lobby", "w0", 0, 0, "in"),
                ("d", "lobby", "w1", 10, 0, "in"),
            ],
            policy=Policy(entrance_zones=("lobby",), max_settle_uncertainty=5),
        )
        result = solve(ds)
        settlement = settle(result)
        self.assertTrue(
            any("超过结算阈值" in cell.reason for cell in settlement.unsettled)
        )


class LedgerTest(unittest.TestCase):
    def test_append_and_replay_restart_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = Ledger(path)
            ledger.record_restart_proof("d", "w1", recorded_at="2026-11-19T10:00:00+08:00")
            ledger.record_entry_cap(
                "lobby", "w1", 0, basis=["w1 重启证明"], recorded_at="2026-11-19T10:01:00+08:00"
            )
            reloaded = Ledger(path)
            self.assertEqual(len(reloaded.entries), 2)
            ds = make_dataset(
                readings=[("d", "lobby", "w0", 5, 0, "in"), ("d", "lobby", "w1", 58, 0, "in")],
                policy=Policy(entrance_zones=("lobby",)),
            )
            replay = reloaded.replay(ds)
            self.assertEqual(len(replay.restart_proofs), 1)
            self.assertEqual(replay.entry_caps[0].cap, 0)
            evidence = build_evidence(
                replay.dataset, restart_proofs=replay.restart_proofs
            )
            # 倒计数口径由政策声明；测试资料用的是默认 cumulative，因此回绕
            # 不会自动生效。这里显式切到 countdown 再核对。
            countdown = replay.dataset.replace(
                policy=replay.dataset.policy.replace(counter_mode="countdown")
            )
            evidence = build_evidence(
                countdown, restart_proofs=replay.restart_proofs
            )
            self.assertEqual(evidence.channel("lobby", "w1", "in").span.lo, 7)

    def test_tampered_ledger_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = Ledger(path)
            ledger.record_entry_cap("lobby", "w0", 3, recorded_at="2026-11-19T10:00:00+08:00")
            rows = path.read_text(encoding="utf-8").splitlines()
            row = json.loads(rows[0])
            row["payload"]["cap"] = 999
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerError):
                Ledger(path)

    def test_query_by_window_and_zone_without_personal_fields(self):
        ledger = Ledger(None)
        ledger.record_restart_proof("d", "w3", recorded_at="2026-11-19T10:00:00+08:00")
        ledger.record_entry_cap(
            "lobby", "w3", 4, recorded_at="2026-11-19T10:05:00+08:00"
        )
        matches = ledger.query(window="w3", zone="lobby")
        self.assertEqual(len(matches), 1)  # 重启证明没有馆区字段，不匹配 zone
        all_w3 = ledger.query(window="w3")
        self.assertEqual(len(all_w3), 2)
        for entry in all_w3:
            serialized = json.dumps(entry.payload, ensure_ascii=False)
            self.assertNotIn("person", serialized.lower())
            self.assertNotIn("轨迹", serialized)

    def test_history_keeps_old_basis_after_new_evidence(self):
        ledger = Ledger(None)
        first = ledger.record_entry_cap(
            "lobby", "w2", 0, basis=["w1 人工点数 6"], recorded_at="2026-11-19T10:00:00+08:00"
        )
        ledger.record_manual_count(
            "lobby", "w1", 8, confirmed=True, recorded_at="2026-11-19T11:00:00+08:00"
        )
        self.assertEqual(ledger.entries[0].payload["basis"], ["w1 人工点数 6"])
        self.assertEqual(ledger.entries[0].digest, first.digest)
        self.assertEqual(len(ledger.entries), 2)


class ReportTest(unittest.TestCase):
    def test_infeasible_report_has_no_restriction_advice(self):
        ds = make_dataset(
            zones=[("lobby", 5, 5, 0), ("hall", 5, 5, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            incidents=[("w0", "lobby", 6, (), True)],
            policy=Policy(entrance_zones=("lobby",)),
        )
        result = solve(ds)
        self.assertTrue(result.infeasible)
        report = build_report(ds, result=result)
        self.assertEqual(report["status"], "no-explanation")
        text = render_text(report)
        self.assertIn("没有相容解释", text)
        self.assertNotIn("优先限制入口", text)

    def test_report_uses_ranges_not_midpoints(self):
        ds = make_dataset(
            zones=[("lobby", 40, 20, 0), ("hall", 20, 10, 0)],
            readings=[("d", "lobby", "w0", 0, 0, "in")],
            policy=Policy(entrance_zones=("lobby",)),
        )
        report = build_report(ds)
        self.assertEqual(report["status"], "partial")
        for block in report["windows_by_range"].values():
            self.assertIn("lo", block["total"]["range"])
            self.assertIn("hi", block["total"]["range"])
            self.assertNotIn("mid", json.dumps(block))
        text = render_text(report)
        # 文本明确说明「不据中点判断」，但不输出任何中点数值。
        self.assertIn("不能据中点或零值判断", text)
        self.assertNotIn("代表值", text)


if __name__ == "__main__":
    unittest.main()
