"""值班建议：软警戒线、入口限制优先级、证据充分性与可承接余量。

原则
====

- **软警戒线与容量不是一回事**：越过 ``soft_margin × capacity`` 只表示需要
  关注，触及 ``capacity`` 才表示必须限流。报告分别给出「可能越过」与
  「确定越过」，不合并成一个等级。
- **先限制哪个入口**由可复核的反事实给出：把某个入口在该窗口起封顶为 0，
  重新求全馆最坏在场人数；下降越多，该入口越关键。这不是凭排序拍脑袋。
- **证据不足与已有余量分开说**：区间上界未知（缺测）是证据不足；上界已知
  且低于容量是局部余量，可承接分流。
- 所有建议只在本地生成，不访问网络、不读取个人轨迹。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .data import Dataset
from .evidence import EvidenceBundle
from .model import EntryCap
from .solve import SolveResult


class Pressure(str, Enum):
    UNKNOWN = "unknown"             # 证据不足，无法判断
    OK = "ok"                       # 上界也低于软线
    SOFT_POSSIBLE = "soft-possible"  # 最坏状态可能越软线
    SOFT_CERTAIN = "soft-certain"    # 最有利状态也已越软线
    CAP_POSSIBLE = "cap-possible"    # 最坏状态可能触及容量
    CAP_CERTAIN = "cap-certain"      # 最有利状态也已超容量


_ORDER = {
    Pressure.OK: 0,
    Pressure.SOFT_POSSIBLE: 1,
    Pressure.SOFT_CERTAIN: 2,
    Pressure.CAP_POSSIBLE: 3,
    Pressure.CAP_CERTAIN: 4,
    Pressure.UNKNOWN: 5,
}


@dataclass(frozen=True)
class ZonePressure:
    zone: str
    window: str
    capacity: int
    soft_limit: float
    span: object
    pressure: Pressure
    headroom_worst: int | None
    evidence_sufficient: bool

    @property
    def needs_restriction(self) -> bool:
        return self.pressure in {Pressure.CAP_POSSIBLE, Pressure.CAP_CERTAIN}

    @property
    def soft_line_crossed(self) -> bool:
        return self.pressure in {
            Pressure.SOFT_POSSIBLE,
            Pressure.SOFT_CERTAIN,
            Pressure.CAP_POSSIBLE,
            Pressure.CAP_CERTAIN,
        }


@dataclass(frozen=True)
class EntranceLever:
    """把某入口在该窗口封顶后的反事实结果。"""

    zone: str
    window: str
    total_before: int
    total_after: int
    zone_before: int | None
    zone_after: int | None
    is_entrance: bool = False

    @property
    def total_drop(self) -> int:
        return self.total_before - self.total_after

    @property
    def useful(self) -> bool:
        return self.total_drop > 0


@dataclass
class Advice:
    windows: dict[str, dict[str, ZonePressure]]
    totals: dict[str, ZonePressure]
    levers: dict[str, list[EntranceLever]]
    notes: list[str] = field(default_factory=list)

    def worst_pressure(self, window: str) -> Pressure:
        levels = [cell.pressure for cell in self.windows[window].values()]
        levels.append(self.totals[window].pressure)
        return max(levels, key=lambda p: _ORDER[p])

    def windows_needing_restriction(self) -> list[str]:
        return [
            window
            for window in self.totals
            if self.worst_pressure(window)
            in {Pressure.CAP_POSSIBLE, Pressure.CAP_CERTAIN}
        ]

    def first_useful_lever(self, window: str) -> EntranceLever | None:
        """最值得先限制的分区入口（按反事实下降排序）。

        只允许真正位于入口馆区的分区；``lobby`` 是入口馆区，其它分区即使
        反事实能局部降人数，也不是「入口」。
        """

        for lever in self.levers.get(window, []):
            if lever.useful and lever.is_entrance:
                return lever
        return None


def _classify(span, capacity: int, soft_limit: float, known: bool) -> Pressure:
    if span.hi is None:
        return Pressure.UNKNOWN
    hi = span.hi
    lo = span.lo
    if lo > capacity:
        return Pressure.CAP_CERTAIN
    if hi > capacity:
        return Pressure.CAP_POSSIBLE
    if lo > soft_limit:
        return Pressure.SOFT_CERTAIN
    if hi > soft_limit:
        return Pressure.SOFT_POSSIBLE
    return Pressure.OK


def _evidence_sufficient(result: SolveResult, zone: str, window: str) -> bool:
    """该区该窗口是否有任何已登记通道证据且未被撤下。"""

    evidence = result.evidence
    for kind in ("in", "out", "cross"):
        channel = evidence.channel(zone, window, kind)
        if channel is not None and channel.devices:
            return True
    return False


def advise(
    result: SolveResult,
    *,
    entrance_zones: tuple[str, ...] | None = None,
    probe_levers: bool = True,
) -> Advice:
    """根据相容范围给出分区压力与入口优先级。"""

    if result.infeasible:
        return Advice({}, {}, {}, list(result.conflicts))

    dataset = result.dataset
    soft = dataset.policy.soft_margin
    windows: dict[str, dict[str, ZonePressure]] = {}
    totals: dict[str, ZonePressure] = {}
    for window in result.windows():
        zone_cells: dict[str, ZonePressure] = {}
        for zone, cell in result.zones[window].items():
            spec = dataset.topology.zones[zone]
            known = _evidence_sufficient(result, zone, window)
            level = _classify(cell.span, spec.capacity, soft * spec.capacity, known)
            if not known and level not in {Pressure.CAP_CERTAIN, Pressure.CAP_POSSIBLE}:
                level = Pressure.UNKNOWN
            zone_cells[zone] = ZonePressure(
                zone=zone,
                window=window,
                capacity=spec.capacity,
                soft_limit=soft * spec.capacity,
                span=cell.span,
                pressure=level,
                headroom_worst=None
                if cell.span.hi is None
                else spec.capacity - cell.span.hi,
                evidence_sufficient=known,
            )
        windows[window] = zone_cells
        total = result.total_range(window)
        totals[window] = ZonePressure(
            zone="__total__",
            window=window,
            capacity=total.capacity,
            soft_limit=soft * total.capacity,
            span=total.span,
            pressure=_classify(total.span, total.capacity, soft * total.capacity, True),
            headroom_worst=total.headroom_worst,
            evidence_sufficient=True,
        )

    entrances = set(entrance_zones or dataset.policy.entrance_zones)
    levers: dict[str, list[EntranceLever]] = {}
    if probe_levers:
        candidates = entrance_zones
        if candidates is None:
            # 只探测入口馆区与出口馆区；没有登记时退化为全部分区并标明。
            candidates = tuple(sorted(entrances | set(dataset.policy.closing_zones))) or tuple(
                sorted(dataset.topology.zones)
            )
        for window in result.windows():
            # 只有需要关注时才做反事实：容量可能/确定超限，或软线可能越线。
            if totals[window].pressure is Pressure.OK:
                zone_pressures = {
                    cell.pressure for cell in windows[window].values()
                }
                if not zone_pressures & {
                    Pressure.SOFT_POSSIBLE,
                    Pressure.SOFT_CERTAIN,
                    Pressure.CAP_POSSIBLE,
                    Pressure.CAP_CERTAIN,
                }:
                    continue
            levers[window] = _probe_entrances(result, window, candidates, entrances)

    notes = []
    unknown = [
        f"{window}/{zone}"
        for window in windows
        for zone, cell in windows[window].items()
        if cell.pressure is Pressure.UNKNOWN
    ]
    if unknown:
        notes.append(
            f"{len(unknown)} 个分区/时窗证据不足（上界未知），不能据中点或零值判断是否超限"
        )
    insufficient = [
        f"{window}/{zone}"
        for window in windows
        for zone, cell in windows[window].items()
        if not cell.evidence_sufficient
        and cell.pressure in {Pressure.CAP_POSSIBLE, Pressure.CAP_CERTAIN}
    ]
    if insufficient:
        notes.append(
            "以下分区已有超限迹象但缺少通道证据，应优先现场复点：" + "、".join(insufficient[:8])
        )
    return Advice(windows, totals, levers, notes)


def _probe_entrances(
    result: SolveResult,
    window: str,
    candidates: tuple[str, ...],
    entrances: set[str],
) -> list[EntranceLever]:
    """逐个把入口在 ``window`` 封顶为 0，观察全馆与各分区最坏在场的下降。

    直接在已构建的可行域上追加一行约束后重解，不重建整份资料。
    """

    model = result.model
    base_total = result.total_range(window).span.hi
    if base_total is None:
        return []
    zone_hi = {
        zone: result.zone_range(zone, window).span.hi for zone in result.zones[window]
    }
    probes: list[EntranceLever] = []
    for zone in candidates:
        if zone not in model.topology.zones:
            continue
        try:
            probe = model.with_entry_cap(zone, window, 0)
            if not probe.feasible():
                raise ValueError("封顶与既有证据冲突")
            after_total, _ = probe.extreme(probe.total_terms(window), maximize=True)
            if zone in zone_hi:
                after_zone, _ = probe.extreme(
                    probe.occupancy_terms(zone, window), maximize=True
                )
            else:
                after_zone = None
        except Exception:
            after_total, after_zone = base_total, None
        probes.append(
            EntranceLever(
                zone=zone,
                window=window,
                total_before=base_total,
                total_after=after_total,
                zone_before=zone_hi.get(zone),
                zone_after=after_zone,
                is_entrance=zone in entrances,
            )
        )
    probes.sort(key=lambda lever: (-lever.total_drop, lever.zone))
    return probes


def solve_scenario(
    dataset: Dataset,
    evidence: EvidenceBundle,
    caps: tuple[EntryCap, ...],
    *,
    only_window: str | None = None,
    closures=(),
) -> dict[str, tuple[int, int | None]]:
    """在不重算全部区间的前提下，返回各窗口「全馆最坏值的上限」。

    用于反事实比较；完整报告仍由 :func:`footfall.solve.solve` 生成。
    """

    from .model import FootfallModel

    model = FootfallModel(dataset, evidence, entry_caps=caps, closures=closures)
    if not model.feasible():
        return {}
    result: dict[str, tuple[int, int | None]] = {}
    windows = [only_window] if only_window else model.windows
    for window in windows:
        if window not in model.windows:
            continue
        value, _ = model.extreme(model.total_terms(window), maximize=True)
        zone_max = max(
            model.extreme(model.occupancy_terms(zone, window), maximize=True)[0]
            for zone in model.zones
        )
        result[window] = (value, zone_max)
    return result
