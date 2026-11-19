"""把拓扑、容量与证据变成线性约束系统。

变量
====

- ``occ[z,w]``：馆区 ``z`` 在窗口 ``w`` **结束时**的在场人数；
- ``entry[z,w]`` / ``exit[z,w]``：该窗口从外部进入/离开馆区 ``z`` 的人数；
- ``flow[e,w,dir]``：该窗口跨过连廊 ``e`` 的人数，双向各一个变量。

约束
====

1. **守恒**：每个馆区每个窗口的在场人数变化，等于外部进出加上各连廊净流入。
   跨区的人在一个区记离开、在相邻区记进入，二者是同一流量变量，不会凭空
   产生人数；全馆净人数只能由外部进出改变。
2. **容量**：``occ[z,w] <= capacity[z]``。
3. **门限**：``entry[z,w] <= door_limit[z]``、``exit[z,w] <= door_limit[z]``。
   门限约束的是每窗口、每方向的门总吞吐，不是单台设备。
4. **连廊**：双向合计 ``flow[e,w,+] + flow[e,w,-] <= max_flow[e]``。
5. **证据**：``in`` 读数约束 ``entry``，``out`` 读数约束 ``exit``，``cross``
   读数约束该馆区所有相邻连廊**双向合计**的通过人数；区间证据给出上下界，
   点证据给出等式。缺测不生成约束，绝不填零。
6. **人工点数**：已确认的点数把 ``occ[z,w]`` 固定为该值；未确认只登记。
7. **关闭**：登记的关闭连廊在该窗口两向流量为零；闭馆窗口所有进出与连廊
   流量为零。
8. **入口限制**：值班员登记的限制把对应入口从生效窗口起的 ``entry`` 封顶。

模型是线性松弛；所有约束系数与右端项为整数，端点按整数向外取整。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import Dataset, window_index
from .errors import DataError
from .evidence import EvidenceBundle
from .interval import Span, outward_round
from .lp import Problem


@dataclass(frozen=True)
class EntryCap:
    """入口限制：自 ``start_window`` 起，``zone`` 的外部进入封顶为 ``cap``。"""

    zone: str
    start_window: str
    cap: int
    label: str = ""

    def __post_init__(self):
        if self.cap < 0:
            raise DataError("入口限制不能为负")
        window_index(self.start_window)


@dataclass(frozen=True)
class EdgeClosure:
    """额外登记的关闭连廊；与事发资料中的关闭记录合并。"""

    edge: str
    start_window: str
    label: str = ""

    def __post_init__(self):
        window_index(self.start_window)


@dataclass
class ModelDiagnostics:
    conflicts: list[str] = field(default_factory=list)
    quarantined: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        if message not in self.conflicts:
            self.conflicts.append(message)


class FootfallModel:
    """构建后可直接用 :meth:`extreme` 求任意线性目标的极值。"""

    def __init__(
        self,
        dataset: Dataset,
        evidence: EvidenceBundle,
        *,
        entry_caps: tuple[EntryCap, ...] = (),
        closures: tuple[EdgeClosure, ...] = (),
        diagnostics: ModelDiagnostics | None = None,
    ):
        self.dataset = dataset
        self.evidence = evidence
        self.entry_caps = tuple(entry_caps)
        self.closures = tuple(closures)
        self.diagnostics = diagnostics or ModelDiagnostics()
        self.topology = dataset.topology
        self.policy = dataset.policy
        self.zones = sorted(self.topology.zones)
        self.windows = [f"w{i}" for i in range(dataset.window_count())]
        self.window_open = {w.label: w.is_open for w in dataset.windows()}
        self._index: dict[str, int] = {}
        self._problem: Problem | None = None
        self._objective_cache: dict[tuple[tuple[str, int], ...], Span] = {}
        self._build()

    # -- 变量布局 -----------------------------------------------------
    def _var(self, name: str) -> int:
        index = self._index.get(name)
        if index is None:
            index = len(self._index)
            self._index[name] = index
        return index

    def occ_var(self, zone: str, window: str) -> int:
        return self._index[f"occ|{zone}|{window}"]

    def entry_var(self, zone: str, window: str) -> int:
        return self._index[f"entry|{zone}|{window}"]

    def exit_var(self, zone: str, window: str) -> int:
        return self._index[f"exit|{zone}|{window}"]

    def flow_var(self, edge: str, window: str, direction: int) -> int:
        return self._index[f"flow|{edge}|{window}|{direction}"]

    # -- 构建 ---------------------------------------------------------
    def _build(self) -> None:
        for zone in self.zones:
            for window in self.windows:
                self._var(f"occ|{zone}|{window}")
                self._var(f"entry|{zone}|{window}")
                self._var(f"exit|{zone}|{window}")
        for edge in sorted(self.topology.edges):
            for window in self.windows:
                self._var(f"flow|{edge}|{window}|1")
                self._var(f"flow|{edge}|{window}|-1")

        problem = Problem(len(self._index))
        self._problem = problem
        blank = lambda: [0.0] * len(self._index)  # noqa: E731

        # 1. 守恒
        for window in self.windows:
            previous = _previous_window(window)
            for zone in self.zones:
                row = blank()
                row[self.occ_var(zone, window)] += 1
                if previous is not None:
                    row[self.occ_var(zone, previous)] -= 1
                row[self.entry_var(zone, window)] -= 1
                row[self.exit_var(zone, window)] += 1
                for edge_name, edge in self.topology.edges.items():
                    # flow(+,edge)=a→b 离开 a、进入 b；flow(-,edge)=b→a 反之。
                    # 守恒式写作 occ - prev - entry + exit + Σ(out) - Σ(in) = 0。
                    if edge.a == zone:
                        row[self.flow_var(edge_name, window, 1)] += 1   # a→b 是离开 a
                        row[self.flow_var(edge_name, window, -1)] -= 1  # b→a 是进入 a
                    elif edge.b == zone:
                        row[self.flow_var(edge_name, window, -1)] += 1   # b→a 是离开 b
                        row[self.flow_var(edge_name, window, 1)] -= 1   # a→b 是进入 b
                rhs = self.topology.zones[zone].initial_count if previous is None else 0
                problem.eq(row, rhs)

        # 2/3. 容量与门限
        for zone in self.zones:
            spec = self.topology.zones[zone]
            for window in self.windows:
                row = blank()
                row[self.occ_var(zone, window)] = 1
                problem.le(row, spec.capacity)
                row = blank()
                row[self.entry_var(zone, window)] = 1
                problem.le(row, spec.door_limit)
                row = blank()
                row[self.exit_var(zone, window)] = 1
                problem.le(row, spec.door_limit)

        # 4. 连廊双向合计
        for edge_name, edge in self.topology.edges.items():
            for window in self.windows:
                row = blank()
                row[self.flow_var(edge_name, window, 1)] = 1
                row[self.flow_var(edge_name, window, -1)] = 1
                problem.le(row, edge.max_flow)

        # 5. 证据
        for (zone, window, kind), channel in sorted(self.evidence.channels.items()):
            if not channel.is_known:
                continue
            for row in self._channel_rows(blank, zone, window, kind):
                problem.ge(row, channel.span.lo)
                if channel.span.hi is not None:
                    problem.le(list(row), channel.span.hi)

        # 6. 人工点数
        for index, zone, value, incident in self.dataset.manual_counts():
            window = f"w{index}"
            row = blank()
            row[self.occ_var(zone, window)] = 1
            problem.eq(row, value)

        # 7. 关闭与闭馆
        closures = self._effective_closures()
        for (edge_name, window), reason in closures.items():
            for direction in (1, -1):
                row = blank()
                row[self.flow_var(edge_name, window, direction)] = 1
                problem.le(row, 0)
        for window in self.windows:
            if self.window_open[window]:
                continue
            for zone in self.zones:
                for var in (
                    self.entry_var(zone, window),
                    self.exit_var(zone, window),
                ):
                    row = blank()
                    row[var] = 1
                    problem.le(row, 0)
            for edge_name in self.topology.edges:
                for direction in (1, -1):
                    row = blank()
                    row[self.flow_var(edge_name, window, direction)] = 1
                    problem.le(row, 0)

        # 7b. 入口馆区封闭：没有外部入口的馆区，外部进出只能为 0。
        #     未登记 entrance_zones 时不施加，避免把「不知道门在哪」当成「没有门」。
        if self.policy.entrance_zones:
            entrances = set(self.policy.entrance_zones)
            for zone in self.zones:
                if zone in entrances:
                    continue
                for window in self.windows:
                    for var in (self.entry_var(zone, window), self.exit_var(zone, window)):
                        row = blank()
                        row[var] = 1
                        problem.le(row, 0)

        # 8. 入口限制
        for cap in self.entry_caps:
            if cap.zone not in self.topology.zones:
                raise DataError(f"入口限制引用未登记馆区 {cap.zone}")
            for window in self.windows:
                if window_index(window) >= window_index(cap.start_window):
                    row = blank()
                    row[self.entry_var(cap.zone, window)] = 1
                    problem.le(row, cap.cap)

    def _effective_closures(self) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for window in self.windows:
            for edge, source in self.dataset.closed_edges_at(window_index(window)).items():
                result[(edge, window)] = f"事发资料 {source} 登记关闭 {edge}"
        for closure in self.closures:
            if closure.edge not in self.topology.edges:
                raise DataError(f"关闭登记引用未登记连廊 {closure.edge}")
            for window in self.windows:
                if window_index(window) >= window_index(closure.start_window):
                    result[(closure.edge, window)] = closure.label or "值班员登记关闭"
        return result

    def _channel_rows(self, blank, zone: str, window: str, kind: str) -> list[list[float]]:
        """一个通道读数约束的是哪股人流。

        - ``in``/``out``：该馆区**闸机/门**的外部进出。每个馆区都可能有自己的
          入口（多入口馆区），因此约束 ``entry``/``exit``。
        - ``cross``：穿过连廊、在相邻两区各计一次的跨区流动；约束该馆区所有
          相邻连廊双向合计的通过人数。一条跨区读数不指明具体连廊，因此不能
          强行摊到某一条上；只有该区**所有**相邻连廊都关闭时才与读数冲突
          （见 :mod:`footfall.evidence` 的机制冲突检查）。

        同一跨区流动在 A 记出、在 B 记入，共享同一个流量变量，不会凭空产生
        人数；全馆净人数只由外部进出改变。
        """

        row = blank()
        if kind == "in":
            row[self.entry_var(zone, window)] += 1
        elif kind == "out":
            row[self.exit_var(zone, window)] += 1
        elif kind == "cross":
            for edge_name, edge in self.topology.edges.items():
                if edge.connects(zone):
                    row[self.flow_var(edge_name, window, 1)] += 1
                    row[self.flow_var(edge_name, window, -1)] += 1
        else:  # pragma: no cover - 载入层已校验
            raise DataError(f"未知通道类别 {kind}")
        return [row]

    # -- 求解 ---------------------------------------------------------
    @property
    def problem(self) -> Problem:
        assert self._problem is not None
        return self._problem

    def feasible(self) -> bool:
        return self.problem.feasible()

    def extreme(
        self, terms: dict[int, float], *, maximize: bool
    ) -> tuple[int, tuple[float, ...]]:
        """表达式极值，按整数口径向外取整。

        ``maximize=True`` 得到上界（向下取整），否则得到下界（向上取整）；
        取整方向保证真实整数可行值一定落在区间内。浮点噪声用容差吸收。
        """

        value, values = self.extreme_float(terms, maximize=maximize)
        return outward_round(value, lower=not maximize), values

    def extreme_float(
        self, terms: dict[int, float], *, maximize: bool
    ) -> tuple[float, tuple[float, ...]]:
        """未取整的表达式极值，供需要精确线性组合（如字典序目标）的调用方使用。"""

        objective = [0.0] * len(self._index)
        for index, value in terms.items():
            objective[index] = value
        if maximize:
            result = self.problem.maximize(objective)
        else:
            result = self.problem.minimize(objective)
        return result.value, result.values

    def occupancy_terms(self, zone: str, window: str) -> dict[int, float]:
        return {self.occ_var(zone, window): 1.0}

    def total_terms(self, window: str) -> dict[int, float]:
        return {self.occ_var(zone, window): 1.0 for zone in self.zones}

    def entry_terms(self, zone: str, window: str) -> dict[int, float]:
        return {self.entry_var(zone, window): 1.0}

    def with_entry_cap(self, zone: str, window: str, cap: int) -> "FootfallModel":
        """返回仅在该窗口把该入口封顶的克隆模型（反事实，不改原模型）。"""

        if zone not in self.topology.zones:
            raise DataError(f"入口封顶引用未登记馆区 {zone}")
        extra_row = [0.0] * len(self._index)
        extra_row[self.entry_var(zone, window)] = 1.0
        clone = object.__new__(FootfallModel)
        clone.__dict__.update(self.__dict__)
        clone._problem = self.problem.clone_with([(extra_row, float(cap), "<=")])
        clone._objective_cache = {}
        return clone


def _previous_window(window: str) -> str | None:
    index = window_index(window)
    return None if index == 0 else f"w{index - 1}"
