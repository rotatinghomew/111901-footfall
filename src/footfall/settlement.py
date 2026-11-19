"""按利用率结算的场地小时。

口径
====

- 只有**证据充分**的分区/时窗才进入结算：该区该窗口至少有一条已登记通道
  证据，且相容区间的宽度不超过政策 ``max_settle_uncertainty``（若设置）。
- 结算量是区间，不是点值：``利用率 = 在场人数 / 容量``，因此
  ``场地小时 ∈ [窗口时长 × lo/容量, 窗口时长 × hi/容量]``。不取中点。
- 上界未知（缺测/撤下读数）的时窗一律不可结算，记入 ``unsettled`` 并说明
  原因，不能用零或最近值顶替。
- 补来更早的人工点数或重启证明会改变可行域；调用方应以新 :class:`SolveResult`
  重新结算，档案中保留旧结算及其依据版本（见 :mod:`footfall.ledger`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .solve import SolveResult


@dataclass(frozen=True)
class SettlementCell:
    zone: str
    window: str
    hours: float
    occupancy: object  # Span
    capacity: int
    billable: bool
    reason: str = ""

    @property
    def utilization_low(self) -> float | None:
        if self.occupancy.hi is None:
            return None
        return self.occupancy.lo / self.capacity if self.capacity else None

    @property
    def utilization_high(self) -> float | None:
        if self.occupancy.hi is None:
            return None
        return self.occupancy.hi / self.capacity if self.capacity else None

    @property
    def venue_hours_low(self) -> float | None:
        if not self.billable or self.occupancy.hi is None:
            return None
        return self.hours * self.occupancy.lo / self.capacity if self.capacity else None

    @property
    def venue_hours_high(self) -> float | None:
        if not self.billable or self.occupancy.hi is None:
            return None
        return self.hours * self.occupancy.hi / self.capacity if self.capacity else None


@dataclass
class Settlement:
    cells: list[SettlementCell]
    unsettled: list[SettlementCell]
    notes: list[str] = field(default_factory=list)

    @property
    def billable_hours_low(self) -> float:
        return sum(cell.venue_hours_low or 0.0 for cell in self.cells)

    @property
    def billable_hours_high(self) -> float:
        return sum(cell.venue_hours_high or 0.0 for cell in self.cells)

    def by_window(self) -> dict[str, list[SettlementCell]]:
        result: dict[str, list[SettlementCell]] = {}
        for cell in self.cells:
            result.setdefault(cell.window, []).append(cell)
        return result

    def summary(self) -> dict[str, float]:
        return {
            "billable_hours_low": round(self.billable_hours_low, 4),
            "billable_hours_high": round(self.billable_hours_high, 4),
            "settled_cells": float(len(self.cells)),
            "unsettled_cells": float(len(self.unsettled)),
        }


def settle(result: SolveResult, *, max_uncertainty: int | None = None) -> Settlement:
    """按相容区间结算场地小时；证据不足的时窗单列。"""

    if result.infeasible:
        return Settlement([], [], ["分析无相容解释，不能结算任何场地小时"])

    dataset = result.dataset
    policy = dataset.policy
    if max_uncertainty is None:
        max_uncertainty = policy.max_settle_uncertainty
    hours_by_window = {w.label: w.hours for w in dataset.windows()}
    cells: list[SettlementCell] = []
    unsettled: list[SettlementCell] = []
    notes: list[str] = []

    for window in result.windows():
        hours = hours_by_window.get(window)
        if hours is None:
            continue
        for zone in sorted(result.zones[window]):
            cell = result.zone_range(zone, window)
            spec = dataset.topology.zones[zone]
            reason = _unsettle_reason(result, zone, window, cell, max_uncertainty)
            record = SettlementCell(
                zone=zone,
                window=window,
                hours=hours,
                occupancy=cell.span,
                capacity=spec.capacity,
                billable=reason == "",
                reason=reason,
            )
            (cells if record.billable else unsettled).append(record)

    if unsettled:
        missing = sum(1 for cell in unsettled if "没有已登记通道证据" in cell.reason)
        unknown = sum(1 for cell in unsettled if "上界未知" in cell.reason)
        wide = sum(1 for cell in unsettled if "超过结算阈值" in cell.reason)
        if missing:
            notes.append(f"{missing} 个分区/时窗没有任何通道证据，未进入结算")
        if unknown:
            notes.append(f"{unknown} 个分区/时窗上界未知（缺测或读数撤下），未进入结算")
        if wide:
            notes.append(
                f"{wide} 个分区/时窗区间宽度超过结算阈值 {max_uncertainty} 人，未进入结算"
            )
    if not notes:
        notes.append("全部时窗均有可结算的相容区间；结算量仍按区间给出，不取中点")
    return Settlement(cells, unsettled, notes)


def _unsettle_reason(
    result: SolveResult, zone: str, window: str, cell, max_uncertainty: int | None
) -> str:
    evidence = result.evidence
    registered = [
        kind
        for kind in ("in", "out", "cross")
        if (channel := evidence.channel(zone, window, kind)) is not None and channel.devices
    ]
    if not registered:
        return "该区该时窗没有已登记通道证据"
    if cell.span.hi is None:
        return "上界未知（缺测或读数撤下），不能结算"
    if max_uncertainty is not None and cell.span.uncertainty is not None:
        if cell.span.uncertainty > max_uncertainty:
            return (
                f"区间宽度 {cell.span.uncertainty} 超过结算阈值 {max_uncertainty}，"
                "需补证据后再结算"
            )
    return ""
