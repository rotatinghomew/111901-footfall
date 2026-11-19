"""累计读数归约：把原始读数变成每个馆区/窗口/通道类别的可行区间。

这里不做「统一填零」也不做「取最近读数」。每种可解释状态都保留为一条
证据并标明依据；证据之间的取舍交给约束系统共同决定。

三类口径
========

``cumulative``（正计数）
    相邻读数差 ``cur - prev`` 即本窗通过人数。``reset=1`` 表示计数器在窗口内
    归零，因此本窗通过人数只能给 ``[0, cur]``，不能填零。

``countdown``（倒计数）
    计数器从 ``ceiling`` 递减到 0 后回绕。``reset=1``（或补来的重启证明）
    表示本窗发生过回绕：通过人数 = ``prev + (ceiling - cur)``；否则
    = ``prev - cur``。回绕判断只依赖已登记的重置标记或重启证明，不靠猜测。

首个窗口
    没有前序读数时，本窗通过人数未知 ``[0, ?]``；除非政策给出
    ``initial_counter_value``（例如开班时计数器满格），才可结算。

缺测与异常
    缺测不生成 0，异常读数保留为宽区间并标注；都不被静默修正。

多设备与重复回报
================

同一馆区/窗口/类别下可能有多台设备。``overlap`` 口径把它们看作同一通道的
冗余回报：取区间包络，绝不相加；``disjoint`` 口径把它们看作不同门：求和。
同一台设备在同一窗口出现多行视为双机重复回报：只保留一次，不一致时标为
冲突，不叠加。

机制冲突
========

读数若只能由已登记关闭的通道产生（``cross`` 读数对应的相邻连廊全部关闭，
或非入口馆区的 ``in``/``out`` 读数在其全部相邻连廊关闭时仍为正），或发生在
闭馆窗口，则该读数与限制措施冲突：撤下该读数、保留限制、标注冲突，
**不**把它当作放宽限制的证据。是否直接停止分析由政策 ``conflict_policy`` 决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .data import Dataset, Reading, window_index
from .interval import Span

KINDS = ("in", "out", "cross")


class State(str, Enum):
    EXACT = "exact"
    RANGE = "range"
    RESET = "reset"
    ANOMALY = "anomaly"
    MISSING = "missing"


@dataclass(frozen=True)
class RestartProof:
    """补来的重启证明：``device`` 在 ``window`` 确认发生过计数器重启/回绕。"""

    device: str
    window: str

    def __post_init__(self):
        window_index(self.window)


@dataclass(frozen=True)
class DeviceStep:
    device: str
    zone: str
    window: str
    kind: str
    span: Span
    state: State
    note: str


@dataclass(frozen=True)
class ChannelEvidence:
    zone: str
    window: str
    kind: str
    span: Span
    devices: tuple[str, ...]
    states: tuple[State, ...]
    aggregation: str
    note: str

    @property
    def is_exact(self) -> bool:
        return self.span.is_point

    @property
    def is_known(self) -> bool:
        return self.span.hi is not None

    @property
    def is_missing(self) -> bool:
        return State.MISSING in self.states

    @property
    def is_suspect(self) -> bool:
        return bool({State.ANOMALY, State.RESET} & set(self.states))


@dataclass(frozen=True)
class ChannelConflict:
    zone: str
    window: str
    kind: str
    reason: str
    evidence: Span


@dataclass
class EvidenceBundle:
    channels: dict[tuple[str, str, str], ChannelEvidence]
    steps: list[DeviceStep]
    warnings: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[ChannelConflict] = field(default_factory=list)

    def channel(self, zone: str, window: str, kind: str) -> ChannelEvidence | None:
        return self.channels.get((zone, window, kind))

    def known_channels(self) -> int:
        return sum(1 for c in self.channels.values() if c.is_known)

    def exact_channels(self) -> int:
        return sum(1 for c in self.channels.values() if c.is_exact)

    def missing_channels(self) -> int:
        return sum(1 for c in self.channels.values() if c.is_missing)

    def conflict_messages(self) -> list[str]:
        return [
            f"{c.window}/{c.zone}/{c.kind}：{c.reason}（读数 {c.evidence} 已撤下，限制措施保留）"
            for c in self.conflicts
        ]


def _device_series(dataset: Dataset, proofs: set[tuple[str, str]]) -> tuple[
    dict[str, list[Reading]], list[str], list[str]
]:
    series: dict[str, list[Reading]] = {}
    duplicates: list[str] = []
    warnings: list[str] = []
    for reading in dataset.readings:
        series.setdefault(reading.device, []).append(reading)
    for device, items in series.items():
        items.sort(key=lambda r: (r.window_index, r.cumulative))
        by_window: dict[int, list[Reading]] = {}
        for item in items:
            by_window.setdefault(item.window_index, []).append(item)
        merged: list[Reading] = []
        for index in sorted(by_window):
            group = by_window[index]
            if len(group) > 1:
                duplicates.append(
                    f"设备 {device} 在 {group[0].window} 有 {len(group)} 条读数，"
                    "按双机重复只计一次"
                )
                if len({(r.cumulative, r.reset, r.kind) for r in group}) > 1:
                    warnings.append(
                        f"设备 {device} 在 {group[0].window} 的重复读数不一致，取包络并标注冲突"
                    )
            merged.append(group[0])
        series[device] = merged
    return series, duplicates, warnings


def _is_restart(reading: Reading, proofs: set[tuple[str, str]]) -> bool:
    return reading.reset or (reading.device, reading.window) in proofs


def _countdown_step(
    previous: Reading, current: Reading, ceiling: int, gap: int, restarted: bool
) -> tuple[Span, State, str]:
    if gap != 1:
        if restarted:
            base = previous.cumulative + (ceiling - current.cumulative)
            high = base + ceiling * (gap - 1)
            return (
                Span(base, high),
                State.RANGE,
                f"设备 {current.device} 跨 {gap} 窗缺测且已重启，"
                f"{current.window} 前合计 {base}..{high} 人",
            )
        return (
            Span(0, ceiling * gap),
            State.RANGE,
            f"设备 {current.device} 跨 {gap} 窗缺测，{current.window} 前合计未知",
        )
    if restarted:
        value = previous.cumulative + (ceiling - current.cumulative)
        if value > ceiling:
            return (
                Span(0, value),
                State.ANOMALY,
                f"设备 {current.device} 在 {current.window} 重启后通过 {value} 人，"
                f"超过倒计数上限 {ceiling}",
            )
        return (
            Span.point(value),
            State.RESET,
            f"设备 {current.device} 在 {current.window} 重启/回绕，通过 {value} 人",
        )
    if current.cumulative > previous.cumulative:
        value = previous.cumulative + (ceiling - current.cumulative)
        return (
            Span(0, value),
            State.ANOMALY,
            f"设备 {current.device} 在 {current.window} 读数上升但未登记重启",
        )
    value = previous.cumulative - current.cumulative
    return (
        Span.point(value),
        State.EXACT,
        f"设备 {current.device} 在 {current.window} 通过 {value} 人",
    )


def _cumulative_step(
    previous: Reading, current: Reading, gap: int, restarted: bool
) -> tuple[Span, State, str]:
    if restarted:
        return (
            Span(0, current.cumulative),
            State.RESET,
            f"设备 {current.device} 在 {current.window} 重置，本窗通过 0..{current.cumulative} 人",
        )
    delta = current.cumulative - previous.cumulative
    if delta < 0:
        return (
            Span(0, previous.cumulative + current.cumulative),
            State.ANOMALY,
            f"设备 {current.device} 在 {current.window} 读数下降 {delta} 且未登记重置",
        )
    if gap == 1:
        return (
            Span.point(delta),
            State.EXACT,
            f"设备 {current.device} 在 {current.window} 通过 {delta} 人",
        )
    return (
        Span(0, delta),
        State.RANGE,
        f"设备 {current.device} 跨 {gap} 窗缺测，{current.window} 前合计 0..{delta} 人",
    )


def _first_step(dataset: Dataset, current: Reading) -> tuple[Span, State, str]:
    policy = dataset.policy
    if policy.initial_counter_value is None:
        return (
            Span(0, None),
            State.MISSING,
            f"设备 {current.device} 在 {current.window} 之前没有读数，本窗通过人数未知",
        )
    if policy.counter_mode == "countdown":
        initial = policy.counter_ceiling if policy.initial_counter_value == "ceiling" else int(
            policy.initial_counter_value
        )
        value = (initial - current.cumulative) % policy.counter_ceiling
        return (
            Span.point(value),
            State.EXACT,
            f"设备 {current.device} 按开班读数 {initial} 结算 {current.window} 通过 {value} 人",
        )
    initial = (
        0
        if policy.initial_counter_value == "ceiling"
        else int(policy.initial_counter_value)
    )
    delta = current.cumulative - initial
    if delta < 0:
        return (
            Span(0, max(current.cumulative, 0)),
            State.ANOMALY,
            f"设备 {current.device} 首个读数低于开班读数 {initial}",
        )
    return (
        Span.point(delta),
        State.EXACT,
        f"设备 {current.device} 按开班读数 {initial} 结算 {current.window} 通过 {delta} 人",
    )


def build_evidence(
    dataset: Dataset,
    *,
    restart_proofs: tuple[RestartProof, ...] = (),
    quarantined: set[tuple[str, str, str]] | None = None,
) -> EvidenceBundle:
    """归约全部读数，并撤下与关闭通道/闭馆时段冲突的读数。"""

    quarantined = set(quarantined or ())
    policy = dataset.policy
    proofs = {(proof.device, proof.window) for proof in restart_proofs}
    for proof in restart_proofs:
        if proof.device not in {r.device for r in dataset.readings}:
            raise ValueError(f"重启证明引用未登记设备 {proof.device}")
    series, duplicates, warnings = _device_series(dataset, proofs)
    steps: list[DeviceStep] = []
    per_slot: dict[tuple[str, str, str], list[DeviceStep]] = {}

    for device in sorted(series):
        items = series[device]
        for order, current in enumerate(items):
            if order == 0:
                span, state, note = _first_step(dataset, current)
            else:
                previous = items[order - 1]
                gap = current.window_index - previous.window_index
                restarted = _is_restart(current, proofs)
                if policy.counter_mode == "countdown":
                    span, state, note = _countdown_step(
                        previous, current, policy.counter_ceiling, gap, restarted
                    )
                else:
                    span, state, note = _cumulative_step(
                        previous, current, gap, restarted
                    )
            step = DeviceStep(device, current.zone, current.window, current.kind, span, state, note)
            steps.append(step)
            per_slot.setdefault((current.zone, current.window, current.kind), []).append(step)

    windows = [f"w{i}" for i in range(dataset.window_count())]
    channels: dict[tuple[str, str, str], ChannelEvidence] = {}
    for zone in sorted(dataset.topology.zones):
        for window in windows:
            for kind in KINDS:
                key = (zone, window, kind)
                if key in quarantined:
                    channels[key] = _blind(zone, window, kind, policy, "已按冲突撤下，待现场复点")
                    continue
                slot = per_slot.get(key, [])
                if not slot:
                    channels[key] = _blind(zone, window, kind, policy, "缺测，未填零")
                    continue
                channels[key] = _aggregate(slot, policy.evidence_aggregation)

    conflicts = _mechanism_conflicts(dataset, channels, policy)
    bundle = EvidenceBundle(channels, steps, warnings, duplicates, conflicts)
    if policy.conflict_policy == "stop" and conflicts:
        bundle.warnings.append(
            f"存在 {len(conflicts)} 条与限制措施冲突的读数，按政策停止给出范围"
        )
    return bundle


def _blind(zone: str, window: str, kind: str, policy, reason: str) -> ChannelEvidence:
    return ChannelEvidence(
        zone, window, kind, Span(0, None), (), (State.MISSING,),
        policy.evidence_aggregation, f"{zone}/{window}/{kind} {reason}",
    )


def _aggregate(slot: list[DeviceStep], aggregation: str) -> ChannelEvidence:
    spans = [step.span for step in slot]
    states = tuple(step.state for step in slot)
    devices = tuple(sorted({step.device for step in slot}))
    zone, window, kind = slot[0].zone, slot[0].window, slot[0].kind
    if aggregation == "disjoint":
        lo = sum(span.lo for span in spans)
        hi = None if any(span.hi is None for span in spans) else sum(
            span.hi for span in spans  # type: ignore[misc]
        )
        note = f"{zone}/{window}/{kind} 按独立门求和：{'+'.join(str(s) for s in spans)}"
    else:
        lo = min(span.lo for span in spans)
        hi = None if any(span.hi is None for span in spans) else max(
            span.hi for span in spans  # type: ignore[misc]
        )
        note = f"{zone}/{window}/{kind} 按冗余回报取包络：{', '.join(str(s) for s in spans)}"
        if len({span for span in spans if span.hi is not None}) > 1:
            note += "（多台设备读数不一致，保留较宽范围）"
    span = Span(lo, hi)  # type: ignore[arg-type]
    if any(state is State.MISSING for state in states):
        span = Span(span.lo, None)
        note += "（含未观测设备，上界未知）"
    return ChannelEvidence(zone, window, kind, span, devices, states, aggregation, note)


def _mechanism_conflicts(dataset: Dataset, channels, policy) -> list[ChannelConflict]:
    """读数与关闭通道/闭馆时段冲突时，撤下读数而不是放宽限制。

    ``in``/``out`` 是该馆区自己闸机上的出入，不依赖连廊，因此连廊关闭不会
    与它们冲突。只有 ``cross``（穿过连廊、在相邻两区各计一次）在该馆区
    **所有**相邻连廊都已登记关闭时，才只能来自关闭通道。
    """

    conflicts: list[ChannelConflict] = []
    for (zone, window, kind), channel in sorted(channels.items()):
        if not channel.is_known or channel.span.hi == 0:
            continue
        index = window_index(window)
        if not dataset.windows()[index].is_open:
            channel_conflict = ChannelConflict(
                zone, window, kind, "闭馆时段仍有流动读数", channel.span
            )
        elif kind == "cross":
            closures = dataset.closed_edges_at(index)
            incident = dataset.topology.incident_edges(zone)
            if not incident or not all(edge in closures for edge in incident):
                continue
            channel_conflict = ChannelConflict(
                zone, window, kind,
                "相邻连廊已全部登记关闭，跨区读数只能来自关闭通道",
                channel.span,
            )
        else:
            continue
        conflicts.append(channel_conflict)
        channels[(zone, window, kind)] = _blind(
            zone, window, kind, policy, "与限制措施冲突，读数已撤下待现场复点"
        )
    return conflicts
