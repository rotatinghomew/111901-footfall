"""相容范围、极值与不可行诊断。

输出的是**约束允许的范围**，不是点估计：每个区、每个窗口和全馆都有
``[下界, 上界]``。区间中点不出现在任何输出里。

「不能同时达到」单独处理
========================

各区单独取到的上界之和可能大于全馆上界，因为不同区的上界由不同解释达到。
这里对每个区额外计算：

- ``conditional_total_max``：先让全馆总数取上界，再最大化该区；
- ``conditional_total_min``：先让全馆总数取下界，再最大化该区。

用的是同一个解释（字典序目标），不是把两个独立解拼在一起。调用方据此
区分「单独可达」与「与全馆极值同时可达」。

不可行时不修补传感器、不放开入口：返回冲突清单与现场复点建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import Dataset
from .evidence import EvidenceBundle, build_evidence
from .interval import Span, outward_round
from .lp import SolverError
from .model import EdgeClosure, EntryCap, FootfallModel, ModelDiagnostics


@dataclass(frozen=True)
class ZoneWindowRange:
    zone: str
    window: str
    span: Span
    capacity: int
    conditional_total_max: Span | None = None
    conditional_total_min: Span | None = None

    @property
    def capacity_breached(self) -> bool:
        """最坏合理状态下可能需要限流（可能超限）。"""

        return self.span.hi is not None and self.span.hi > self.capacity

    @property
    def capacity_certain(self) -> bool:
        """即使最有利的相容解释也已超限。"""

        return self.span.lo > self.capacity

    @property
    def joint_inconsistent(self) -> bool:
        """单独上界与「全馆取上界时」的上界不同，说明极值不能同时达到。"""

        conditional = self.conditional_total_max
        if conditional is None or conditional.hi is None or self.span.hi is None:
            return False
        return conditional.hi != self.span.hi


@dataclass(frozen=True)
class TotalRange:
    window: str
    span: Span
    capacity: int

    @property
    def capacity_breached(self) -> bool:
        return self.span.hi is not None and self.span.hi > self.capacity

    @property
    def capacity_certain(self) -> bool:
        return self.span.lo > self.capacity

    @property
    def headroom_worst(self) -> int | None:
        """最不利解释下还余多少空间；负数表示已超限。"""

        return None if self.span.hi is None else self.capacity - self.span.hi


@dataclass
class SolveResult:
    dataset: Dataset
    evidence: EvidenceBundle
    model: FootfallModel
    zones: dict[str, dict[str, ZoneWindowRange]]
    totals: dict[str, TotalRange]
    entry_caps: tuple[EntryCap, ...]
    diagnostics: ModelDiagnostics
    infeasible: bool = False
    conflicts: tuple[str, ...] = ()
    witness_total_max: dict[str, tuple[float, ...]] = field(default_factory=dict)

    # -- 便捷访问 -----------------------------------------------------
    def windows(self) -> list[str]:
        return list(self.totals)

    def zone_range(self, zone: str, window: str) -> ZoneWindowRange:
        return self.zones[window][zone]

    def total_range(self, window: str) -> TotalRange:
        return self.totals[window]

    def zone_capacity_issues(self, window: str) -> list[ZoneWindowRange]:
        return [
            self.zones[window][zone]
            for zone in sorted(self.zones[window])
            if self.zones[window][zone].capacity_breached
        ]

    def total_capacity(self) -> int:
        return sum(zone.capacity for zone in self.dataset.topology.zones.values())

    def uncertain_cells(self) -> int:
        return sum(
            1
            for cell_map in self.zones.values()
            for cell in cell_map.values()
            if cell.span.uncertainty != 0
        )

    def any_breach(self) -> bool:
        return any(
            cell.capacity_breached for cell_map in self.zones.values() for cell in cell_map.values()
        ) or any(total.capacity_breached for total in self.totals.values())


def solve(
    dataset: Dataset,
    *,
    evidence: EvidenceBundle | None = None,
    entry_caps: tuple[EntryCap, ...] = (),
    closures: tuple[EdgeClosure, ...] = (),
    diagnostics: ModelDiagnostics | None = None,
    conditional: bool = True,
) -> SolveResult:
    """求相容范围。不可行时返回 ``infeasible=True`` 的结果，不抛异常。"""

    evidence = evidence or build_evidence(dataset)
    diagnostics = diagnostics or ModelDiagnostics()
    model = FootfallModel(
        dataset,
        evidence,
        entry_caps=entry_caps,
        closures=closures,
        diagnostics=diagnostics,
    )

    if not model.feasible():
        conflicts = diagnostics.conflicts or _diagnose_infeasibility(
            dataset, evidence, entry_caps, closures
        )
        return SolveResult(
            dataset=dataset,
            evidence=evidence,
            model=model,
            zones={},
            totals={},
            entry_caps=tuple(entry_caps),
            diagnostics=diagnostics,
            infeasible=True,
            conflicts=tuple(conflicts),
        )

    zones: dict[str, dict[str, ZoneWindowRange]] = {}
    totals: dict[str, TotalRange] = {}
    witnesses: dict[str, tuple[float, ...]] = {}
    total_capacity = sum(z.capacity for z in model.topology.zones.values())

    for window in model.windows:
        total_lo_f, _ = model.extreme_float(model.total_terms(window), maximize=False)
        total_hi_f, witness = model.extreme_float(model.total_terms(window), maximize=True)
        witnesses[window] = witness
        total_lo = outward_round(total_lo_f, lower=True)
        total_hi = outward_round(total_hi_f, lower=False)
        if total_hi < total_lo:
            raise SolverError(f"{window} 的全馆下界 {total_lo} 高于上界 {total_hi}")
        zone_cells: dict[str, ZoneWindowRange] = {}
        for zone in model.zones:
            lo_f, _ = model.extreme_float(model.occupancy_terms(zone, window), maximize=False)
            hi_f, _ = model.extreme_float(model.occupancy_terms(zone, window), maximize=True)
            conditional_max = conditional_min = None
            if conditional:
                conditional_max = _conditional_span(
                    model, window, zone, total_hi_f, total_lo_f, prefer="max"
                )
                conditional_min = _conditional_span(
                    model, window, zone, total_hi_f, total_lo_f, prefer="min"
                )
            zone_cells[zone] = ZoneWindowRange(
                zone=zone,
                window=window,
                span=Span(outward_round(lo_f, lower=True), outward_round(hi_f, lower=False)),
                capacity=model.topology.zones[zone].capacity,
                conditional_total_max=conditional_max,
                conditional_total_min=conditional_min,
            )
        zones[window] = zone_cells
        totals[window] = TotalRange(window, Span(total_lo, total_hi), total_capacity)

    return SolveResult(
        dataset=dataset,
        evidence=evidence,
        model=model,
        zones=zones,
        totals=totals,
        entry_caps=tuple(entry_caps),
        diagnostics=diagnostics,
        witness_total_max=witnesses,
    )


def _conditional_span(
    model: FootfallModel,
    window: str,
    zone: str,
    target_max: float,
    target_min: float,
    *,
    prefer: str,
) -> Span:
    """先锁定全馆总数的极值，再求该区的完整范围（同一解释）。

    字典序实现：目标 ``BIG * (±全馆总数) ± 该区人数``，``BIG`` 大于任何单区
    人数，因此先取全馆极值，再在同一最优面上取该区的两个方向：:

        上界 = max(BIG·T + z) − BIG·T*
        下界 = BIG·T* − max(BIG·T − z)

    ``prefer="max"`` 取 ``T* = total_max``（``T`` 前系数为正），
    ``prefer="min"`` 取 ``T* = total_min``（系数为负，等价于先最小化总数）。
    """

    capacity = sum(z.capacity for z in model.topology.zones.values())
    big = float(capacity + 1)
    total_terms = model.total_terms(window)
    zone_terms = model.occupancy_terms(zone, window)
    sign = 1.0 if prefer == "max" else -1.0
    target = target_max if prefer == "max" else target_min

    def run(zone_sign: float) -> float:
        weights = {index: sign * big for index in total_terms}
        for index, value in zone_terms.items():
            weights[index] = weights.get(index, 0.0) + zone_sign * value
        value, _ = model.extreme_float(weights, maximize=True)
        return value

    upper_raw = run(+1.0) - sign * big * target
    lower_raw = sign * big * target - run(-1.0)
    low_r = outward_round(lower_raw, lower=True)
    high_r = outward_round(upper_raw, lower=False)
    if low_r > high_r:
        low_r, high_r = high_r, low_r
    return Span(max(0, low_r), max(0, high_r))


def _diagnose_infeasibility(
    dataset: Dataset,
    evidence: EvidenceBundle,
    entry_caps: tuple[EntryCap, ...],
    closures: tuple[EdgeClosure, ...],
) -> list[str]:
    """找出删掉后能让系统变可行的最小证据集合，用于现场复点。

    只在不可行时调用，且只做「逐条撤下已登记通道」的贪心最小化；不改动
    传感器读数、不放开入口。
    """

    known = [
        (zone, window, kind)
        for (zone, window, kind), channel in evidence.channels.items()
        if channel.is_known
    ]
    if not _feasible_keeping(dataset, evidence, set(), entry_caps, closures):
        return [
            "在没有任何通道读数证据的情况下仍不可行；"
            "请先核对拓扑、容量、人工点数与关闭登记，不要修正传感器"
        ]

    subset = list(known)
    for label in list(known):
        trial = [item for item in subset if item != label]
        if not _feasible_keeping(dataset, evidence, set(trial), entry_caps, closures):
            subset = trial
    labels = [f"{zone}/{window}/{kind}" for zone, window, kind in subset]
    if len(labels) == 1:
        message = f"撤下 {labels[0]} 一条读数后即有相容解释，该读数最可疑"
    else:
        message = (
            "以下读数组合无法与拓扑、容量、关闭登记同时成立："
            + "、".join(labels)
            + "；单独撤下其中任何一条都不够，需现场复点"
        )
    return [message]


def _feasible_keeping(
    dataset: Dataset,
    evidence: EvidenceBundle,
    keep: set[tuple[str, str, str]],
    entry_caps: tuple[EntryCap, ...],
    closures: tuple[EdgeClosure, ...],
) -> bool:
    """仅保留 ``keep`` 中通道证据时的可行性。"""

    quarantined = {
        key
        for key, channel in evidence.channels.items()
        if channel.is_known and key not in keep
    }
    trimmed = build_evidence(dataset, quarantined=quarantined)
    try:
        model = FootfallModel(
            dataset, trimmed, entry_caps=entry_caps, closures=closures
        )
    except Exception:  # pragma: no cover - 构建期错误另行处理
        return False
    return model.feasible()
