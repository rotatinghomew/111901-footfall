"""拓扑、政策与 CSV 资料载入。

字段口径
========

``zones.csv``
    ``capacity``
        该馆区**同时容纳**人数上限（分区容量）。
    ``door_limit``
        该馆区门/闸机**每窗口、每方向**可通过人数上限。一个馆区可能有多扇门、
        多台计数器；``door_limit`` 约束的是每个方向的门总吞吐，而不是单台设备。
    ``initial_count``
        开班时刻（``w0`` 之前）馆区内的人数，作为占用守恒的初始值。

``edges.csv``
    ``max_flow``
        该连廊/门**每窗口双向合计**可同时通过的人数；两个方向共享这一上限。
    边无向；方向由求解变量表达。

``readings.csv``
    ``cumulative``
        设备累计读数。``reset=1`` 表示该行是计数器重置后的第一段读数；
        累计口径（正计数或倒计数）由政策 ``counter_mode`` 声明，不由程序猜测。
    ``kind``
        ``in``/``out`` 分别表示进入/离开该馆区的门计数；``cross`` 表示穿过
        连廊、在两个相邻馆区各计一次的跨区流动。

``incidents.csv``
    ``manual_count``
        人工盘点人数；只有 ``confirmed=1`` 才作为硬证据，``confirmed=0`` 仅登记。
    ``closed_edges``
        值班员登记的关闭通道（``;`` 分隔），自该窗口起按政策生效。

空字段表示该资料没有登记值，不据此推断业务事实。载入阶段只校验字段、
引用与取值，业务取舍在证据层与求解层完成。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Iterable

from .errors import DataError

_WINDOW_RE = re.compile(r"^w(\d+)$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

KINDS = ("in", "out", "cross")


@dataclass(frozen=True)
class Zone:
    name: str
    capacity: int
    door_limit: int
    initial_count: int


@dataclass(frozen=True)
class Edge:
    name: str
    a: str
    b: str
    max_flow: int

    def connects(self, zone: str) -> bool:
        return zone in (self.a, self.b)

    def other(self, zone: str) -> str:
        if zone == self.a:
            return self.b
        if zone == self.b:
            return self.a
        raise DataError(f"edge {self.name} 不连接 {zone}")


@dataclass(frozen=True)
class Reading:
    device: str
    zone: str
    window: str
    cumulative: int
    reset: bool
    kind: str

    @property
    def window_index(self) -> int:
        return window_index(self.window)


@dataclass(frozen=True)
class Incident:
    window: str
    zone: str
    manual_count: int | None
    closed_edges: tuple[str, ...]
    confirmed: bool

    @property
    def window_index(self) -> int:
        return window_index(self.window)


def _parse_time(value: str, name: str) -> time:
    match = _TIME_RE.match(value)
    if not match:
        raise DataError(f"{name} 必须是 HH:MM（24 小时制）：{value!r}")
    return time(int(match.group(1)), int(match.group(2)))


@dataclass(frozen=True)
class Policy:
    """可复核的运行口径。

    所有会影响结论的选择都必须来自政策文件或显式参数，程序不自行猜测：

    ``counter_mode``
        ``cumulative``（正计数，默认）或 ``countdown``（倒计数，需 ``counter_ceiling``）。
    ``counter_ceiling``
        倒计数设备的重置上限；``cumulative`` 模式下忽略。
    ``closure_persistence``
        ``forward``：关闭自登记窗口起持续生效（默认，符合「限量措施改变接下来
        允许开放的通道」）；``window``：仅该窗口生效。
    ``evidence_aggregation``
        ``overlap``：同一馆区/窗口/类别下多台设备视为对同一通道的重叠回报，
        取区间包络（默认，不把重复回报相加）；``disjoint``：视为不同通道求和。
    ``conflict_policy``
        ``flag``：登记与读数冲突时保留限制、撤下冲突读数并标注（默认）；
        ``stop``：冲突即停止输出范围，直接要求复点。
    ``opening_windows`` / ``open_from`` / ``open_until`` / ``day_start``
        场馆开闭时段；未登记开闭时段时全部窗口按开放处理。
    """

    normal_step: int = 1
    window_minutes: int = 15
    soft_margin: float = 0.85
    entrance_zones: tuple[str, ...] = ()
    closing_zones: tuple[str, ...] = ()
    opening_windows: tuple[str, ...] = ()
    open_from: str = ""
    open_until: str = ""
    day_start: str = "08:00"
    counter_mode: str = "cumulative"
    counter_ceiling: int = 60
    initial_counter_value: int | str | None = None
    closure_persistence: str = "forward"
    evidence_aggregation: str = "overlap"
    conflict_policy: str = "flag"
    max_settle_uncertainty: int | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "Policy":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(value) - allowed
        if unknown:
            raise DataError(f"政策包含未知字段：{', '.join(sorted(unknown))}")
        opening = value.get("opening_windows", ())
        if isinstance(opening, str):
            raise DataError("opening_windows 必须是窗口数组")
        entrance = value.get("entrance_zones", ())
        if isinstance(entrance, str):
            raise DataError("entrance_zones 必须是馆区数组")
        closing = value.get("closing_zones", ())
        if isinstance(closing, str):
            raise DataError("closing_zones 必须是馆区数组")
        policy = cls(
            normal_step=int(value.get("normal_step", 1)),
            window_minutes=int(value.get("window_minutes", 15)),
            soft_margin=float(value.get("soft_margin", 0.85)),
            entrance_zones=tuple(str(v) for v in entrance),
            closing_zones=tuple(str(v) for v in closing),
            opening_windows=tuple(str(v) for v in opening),
            open_from=str(value.get("open_from", "")),
            open_until=str(value.get("open_until", "")),
            day_start=str(value.get("day_start", "08:00")),
            counter_mode=str(value.get("counter_mode", "cumulative")),
            counter_ceiling=int(value.get("counter_ceiling", 60)),
            initial_counter_value=value.get("initial_counter_value"),
            closure_persistence=str(value.get("closure_persistence", "forward")),
            evidence_aggregation=str(value.get("evidence_aggregation", "overlap")),
            conflict_policy=str(value.get("conflict_policy", "flag")),
            max_settle_uncertainty=value.get("max_settle_uncertainty"),
        )
        policy.validate()
        return policy

    @classmethod
    def load(cls, path: Path) -> "Policy":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise DataError(f"{path.name} 必须是 JSON 对象")
        return cls.from_dict(raw)

    def replace(self, **changes) -> "Policy":
        data = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        data.update(changes)
        return Policy(**data)

    def validate(self) -> None:
        if self.normal_step <= 0:
            raise DataError("normal_step 必须为正整数")
        if self.window_minutes <= 0:
            raise DataError("window_minutes 必须为正")
        if not 0.0 < self.soft_margin <= 1.0:
            raise DataError("soft_margin 必须在 (0, 1] 内")
        if self.counter_mode not in {"cumulative", "countdown"}:
            raise DataError("counter_mode 必须是 cumulative 或 countdown")
        if self.counter_ceiling <= 0:
            raise DataError("counter_ceiling 必须为正")
        if self.initial_counter_value is not None:
            if self.initial_counter_value != "ceiling" and not isinstance(
                self.initial_counter_value, int
            ):
                raise DataError("initial_counter_value 必须是整数或 'ceiling'")
            if isinstance(self.initial_counter_value, int) and self.initial_counter_value < 0:
                raise DataError("initial_counter_value 不能为负")
        if self.closure_persistence not in {"forward", "window"}:
            raise DataError("closure_persistence 必须是 forward 或 window")
        if self.evidence_aggregation not in {"overlap", "disjoint"}:
            raise DataError("evidence_aggregation 必须是 overlap 或 disjoint")
        if self.conflict_policy not in {"flag", "stop"}:
            raise DataError("conflict_policy 必须是 flag 或 stop")
        if (self.open_from and not self.open_until) or (self.open_until and not self.open_from):
            raise DataError("open_from 与 open_until 必须同时登记")
        if self.open_from:
            _parse_time(self.open_from, "open_from")
            _parse_time(self.open_until, "open_until")
        _parse_time(self.day_start, "day_start")
        if self.open_from and _parse_time(self.open_from, "open_from") > _parse_time(
            self.open_until, "open_until"
        ):
            raise DataError("open_from 不能晚于 open_until")
        for window in self.opening_windows:
            window_index(window)
        if self.max_settle_uncertainty is not None and self.max_settle_uncertainty < 0:
            raise DataError("max_settle_uncertainty 不能为负")


@dataclass(frozen=True)
class Topology:
    zones: dict[str, Zone]
    edges: dict[str, Edge]

    @classmethod
    def build(cls, zones: Iterable[Zone], edges: Iterable[Edge]) -> "Topology":
        zone_list = list(zones)
        zone_map = {z.name: z for z in zone_list}
        if len(zone_map) != len(zone_list):
            raise DataError("zone 名称重复")
        if not zone_map:
            raise DataError("没有任何馆区")
        edge_map: dict[str, Edge] = {}
        for edge in edges:
            if edge.name in edge_map:
                raise DataError(f"edge 名称重复：{edge.name}")
            for endpoint in (edge.a, edge.b):
                if endpoint not in zone_map:
                    raise DataError(f"edge {edge.name} 引用了未登记馆区 {endpoint}")
            if edge.a == edge.b:
                raise DataError(f"edge {edge.name} 不能自连")
            if edge.max_flow < 0:
                raise DataError(f"edge {edge.name} 的 max_flow 不能为负")
            edge_map[edge.name] = edge
        for zone in zone_list:
            if zone.capacity < 0 or zone.door_limit < 0 or zone.initial_count < 0:
                raise DataError(f"{zone.name} 的容量/门限/初始人数不能为负")
            if zone.initial_count > zone.capacity:
                raise DataError(f"{zone.name} 初始人数超过容量")
        return cls(zone_map, edge_map)

    def incident_edges(self, zone: str) -> list[str]:
        return sorted(name for name, edge in self.edges.items() if edge.connects(zone))

    def neighbours(self, zone: str) -> list[str]:
        return sorted({edge.other(zone) for edge in self.edges.values() if edge.connects(zone)})

    def edge_for(self, left: str, right: str) -> Edge | None:
        for edge in self.edges.values():
            if {edge.a, edge.b} == {left, right}:
                return edge
        return None


@dataclass(frozen=True)
class Window:
    label: str
    index: int
    start_minute: int
    end_minute: int
    is_open: bool

    @property
    def hours(self) -> float:
        return (self.end_minute - self.start_minute) / 60.0


def window_index(window: str) -> int:
    match = _WINDOW_RE.match(window)
    if not match:
        raise DataError(f"窗口必须形如 w0、w1：{window!r}")
    return int(match.group(1))


def window_label(index: int) -> str:
    return f"w{index}"


@dataclass
class Dataset:
    topology: Topology
    readings: list[Reading]
    incidents: list[Incident]
    policy: Policy = field(default_factory=Policy)
    source: str = ""

    # -- 载入 ---------------------------------------------------------
    @classmethod
    def load(
        cls,
        *,
        zones: Path,
        edges: Path,
        readings: Path,
        incidents: Path,
        policy: Policy | None = None,
    ) -> "Dataset":
        topology = Topology.build(_load_zones(zones), _load_edges(edges))
        policy = policy or Policy()
        policy.validate()
        loaded_readings = _load_readings(readings)
        loaded_incidents = list(_load_incidents(incidents))

        for reading in loaded_readings:
            if reading.zone not in topology.zones:
                raise DataError(
                    f"读数 {reading.device}/{reading.window} 引用未登记馆区 {reading.zone}"
                )
            if reading.cumulative < 0:
                raise DataError(f"读数 {reading.device}/{reading.window} 的累计值不能为负")
        for incident in loaded_incidents:
            if incident.zone not in topology.zones:
                raise DataError(f"事发资料引用未登记馆区 {incident.zone}")
            for edge in incident.closed_edges:
                if edge not in topology.edges:
                    raise DataError(f"事发资料引用未登记连廊 {edge}")
            if incident.manual_count is not None and incident.manual_count < 0:
                raise DataError(f"{incident.window}/{incident.zone} 人工点数为负")
            if incident.manual_count is not None and incident.manual_count > topology.zones[
                incident.zone
            ].capacity:
                raise DataError(
                    f"{incident.window}/{incident.zone} 人工点数超过分区容量"
                    "（若为真实记录，应先复核容量而不是截断人数）"
                )

        dataset = cls(topology, loaded_readings, loaded_incidents, policy)
        for zone in policy.entrance_zones:
            if zone not in topology.zones:
                raise DataError(f"entrance_zones 引用未登记馆区 {zone}")
        for zone in policy.closing_zones:
            if zone not in topology.zones:
                raise DataError(f"closing_zones 引用未登记馆区 {zone}")
        if policy.counter_mode == "countdown":
            for reading in loaded_readings:
                if reading.cumulative > policy.counter_ceiling:
                    raise DataError(
                        f"读数 {reading.device}/{reading.window}={reading.cumulative} "
                        f"超过倒计数上限 {policy.counter_ceiling}"
                    )
        return dataset

    @classmethod
    def from_dir(cls, directory: Path, policy: Policy | None = None) -> "Dataset":
        directory = Path(directory)
        policy_path = directory / "policy.json"
        if policy is None and policy_path.exists():
            policy = Policy.load(policy_path)
        return cls.load(
            zones=directory / "zones.csv",
            edges=directory / "edges.csv",
            readings=directory / "readings.csv",
            incidents=directory / "incidents.csv",
            policy=policy,
        )

    # -- 派生 ---------------------------------------------------------
    def replace(
        self,
        *,
        readings: list[Reading] | None = None,
        incidents: list[Incident] | None = None,
        policy: Policy | None = None,
    ) -> "Dataset":
        return Dataset(
            self.topology,
            list(self.readings if readings is None else readings),
            list(self.incidents if incidents is None else incidents),
            self.policy if policy is None else policy,
            self.source,
        )

    def window_count(self) -> int:
        highest = -1
        for reading in self.readings:
            highest = max(highest, reading.window_index)
        for incident in self.incidents:
            highest = max(highest, incident.window_index)
        # 没有任何读数时仍存在至少一个开馆窗口；空证据不等于「无窗口」。
        return max(highest, 0) + 1

    def windows(self) -> list[Window]:
        count = self.window_count()
        start = _parse_time(self.policy.day_start, "day_start")
        base = start.hour * 60 + start.minute
        open_from = (
            _parse_time(self.policy.open_from, "open_from") if self.policy.open_from else None
        )
        open_until = (
            _parse_time(self.policy.open_until, "open_until") if self.policy.open_until else None
        )
        windows = []
        for index in range(count):
            begin = base + index * self.policy.window_minutes
            end = begin + self.policy.window_minutes
            if self.policy.opening_windows:
                is_open = window_label(index) in self.policy.opening_windows
            elif open_from is not None and open_until is not None:
                open_lo = open_from.hour * 60 + open_from.minute
                open_hi = open_until.hour * 60 + open_until.minute
                is_open = begin >= open_lo and end <= open_hi
            else:
                is_open = True
            windows.append(Window(window_label(index), index, begin, end, is_open))
        return windows

    def entrance_zones(self) -> tuple[str, ...]:
        if self.policy.entrance_zones:
            return self.policy.entrance_zones
        return ()

    def closed_edges_at(self, window_index_value: int) -> dict[str, str]:
        """返回该窗口生效的关闭通道及其登记窗口。

        按 ``closure_persistence``：``forward`` 表示登记后持续生效；
        ``window`` 表示仅登记窗口生效。
        """
        closures: dict[str, str] = {}
        for incident in sorted(self.incidents, key=lambda i: i.window_index):
            if not incident.confirmed or not incident.closed_edges:
                continue
            active = (
                incident.window_index <= window_index_value
                if self.policy.closure_persistence == "forward"
                else incident.window_index == window_index_value
            )
            if active:
                for edge in incident.closed_edges:
                    closures[edge] = incident.window
        return closures

    def manual_counts(self) -> list[tuple[int, str, int, Incident]]:
        """已确认的人工点数：``(窗口序号, 馆区, 人数, 来源)``。"""

        result = []
        for incident in self.incidents:
            if incident.confirmed and incident.manual_count is not None:
                result.append(
                    (incident.window_index, incident.zone, incident.manual_count, incident)
                )
        return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DataError(f"缺少资料文件：{path.name}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise DataError(f"{path.name} 缺少首行字段名")
        rows = []
        for lineno, raw in enumerate(reader, start=2):
            if all(not (value or "").strip() for value in raw.values()):
                continue
            cleaned = {
                key: (value.strip() if isinstance(value, str) else value)
                for key, value in raw.items()
            }
            cleaned["_line"] = str(lineno)
            rows.append(cleaned)
    return rows


def _require(row: dict[str, str], name: str, source: str) -> str:
    value = row.get(name)
    if value is None or value == "":
        raise DataError(f"{source} 第 {row['_line']} 行缺少 {name}")
    return value


def _int(row: dict[str, str], name: str, source: str, *, minimum: int | None = None) -> int:
    raw = _require(row, name, source)
    try:
        value = int(raw)
    except ValueError as exc:
        raise DataError(f"{source} 第 {row['_line']} 行 {name} 不是整数：{raw}") from exc
    if minimum is not None and value < minimum:
        raise DataError(f"{source} 第 {row['_line']} 行 {name} 不能小于 {minimum}")
    return value


def _load_zones(path: Path) -> list[Zone]:
    zones = []
    for row in _read_csv(path):
        zones.append(
            Zone(
                name=_require(row, "zone", path.name),
                capacity=_int(row, "capacity", path.name, minimum=0),
                door_limit=_int(row, "door_limit", path.name, minimum=0),
                initial_count=_int(row, "initial_count", path.name, minimum=0),
            )
        )
    if not zones:
        raise DataError(f"{path.name} 没有馆区记录")
    return zones


def _load_edges(path: Path) -> list[Edge]:
    return [
        Edge(
            name=_require(row, "edge", path.name),
            a=_require(row, "from", path.name),
            b=_require(row, "to", path.name),
            max_flow=_int(row, "max_flow", path.name, minimum=0),
        )
        for row in _read_csv(path)
    ]


def _load_readings(path: Path) -> list[Reading]:
    readings = []
    for row in _read_csv(path):
        kind = _require(row, "kind", path.name)
        if kind not in KINDS:
            raise DataError(f"{path.name} 第 {row['_line']} 行 kind 非法：{kind}")
        reset_raw = _require(row, "reset", path.name)
        if reset_raw not in {"0", "1"}:
            raise DataError(f"{path.name} 第 {row['_line']} 行 reset 必须是 0/1")
        readings.append(
            Reading(
                device=_require(row, "device", path.name),
                zone=_require(row, "zone", path.name),
                window=_require(row, "window", path.name),
                cumulative=_int(row, "cumulative", path.name, minimum=0),
                reset=reset_raw == "1",
                kind=kind,
            )
        )
    return readings


def _load_incidents(path: Path) -> Iterable[Incident]:
    for row in _read_csv(path):
        closed = tuple(
            part.strip()
            for part in (row.get("closed_edges") or "").split(";")
            if part.strip()
        )
        manual_raw = (row.get("manual_count") or "").strip()
        manual = None if manual_raw == "" else int(manual_raw)
        confirmed_raw = _require(row, "confirmed", path.name)
        if confirmed_raw not in {"0", "1"}:
            raise DataError(f"{path.name} 第 {row['_line']} 行 confirmed 必须是 0/1")
        yield Incident(
            window=_require(row, "window", path.name),
            zone=_require(row, "zone", path.name),
            manual_count=manual,
            closed_edges=closed,
            confirmed=confirmed_raw == "1",
        )
