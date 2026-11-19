"""纯标准库的小型线性规划（两阶段单纯形）。

客流约束都是整数系数的小型系统。求解器只给出可行域的极值端点，不参与
业务取舍：不可行就报不可行，不修补约束；无界就报无界，不截断成有限值。
调用侧负责把端点按整数口径向外取整并解释证据来源。

实现要点
--------

- 两阶段法：第一阶段最小化人工变量之和，第二阶段在此基上继续优化；
- 稠密行 + 按列非零索引：每次主元只改动「在入基列上非零」的行，避免
  遍历全部约束；
- 检验数行增量维护：主元后 ``z ← z - (z_e / a_re)·row_r``，每次迭代
  ``O(列数)``，而不是重算 ``c_B·B⁻¹A``；
- 选主元：Dantzig 规则为主，连续无改进时切换 Bland 规则跳出退化循环；
- 一次构建、多次换目标，复用同一可行基。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-9
_FEAS_TOL = 1e-6
_TIE_TOL = 1e-9
_STALL_LIMIT = 25


class Infeasible(Exception):
    """约束系统没有可行解。"""


class Unbounded(Exception):
    """目标函数在可行域上无界。"""


class SolverError(Exception):
    """求解器内部失败（不应发生）。"""


@dataclass(frozen=True)
class Solution:
    value: float
    values: tuple[float, ...]


class Problem:
    """一次构建、反复更换目标求解同一可行域。"""

    def __init__(self, nvars: int):
        if nvars <= 0:
            raise ValueError("变量数必须为正")
        self.nvars = nvars
        self._raw: list[tuple[list[float], float, str]] = []
        self._built = False
        self.rows: list[list[float]] = []
        self.rhs: list[float] = []
        self.basis: list[int] = []
        self.ncols = 0
        self._artificial: list[int] = []
        self._structural: list[int] = []
        self._blocked: set[int] = set()
        self._phase1_done = False

    # -- 构建 ---------------------------------------------------------
    def add(self, coefficients: list[float], rhs: float, sense: str = "<=") -> None:
        if self._built:
            raise RuntimeError("约束系统已构建，不能再追加")
        if len(coefficients) != self.nvars:
            raise ValueError(f"系数维数应为 {self.nvars}，实际 {len(coefficients)}")
        if sense not in {"<=", ">=", "="}:
            raise ValueError(f"未知约束方向：{sense}")
        if not math.isfinite(rhs):
            raise ValueError("右端项必须有限")
        for value in coefficients:
            if not math.isfinite(value):
                raise ValueError("系数必须有限")
        self._raw.append(([float(v) for v in coefficients], float(rhs), sense))

    def le(self, coefficients: list[float], rhs: float) -> None:
        self.add(coefficients, rhs, "<=")

    def ge(self, coefficients: list[float], rhs: float) -> None:
        self.add(coefficients, rhs, ">=")

    def eq(self, coefficients: list[float], rhs: float) -> None:
        self.add(coefficients, rhs, "=")

    # -- 构建内部 -----------------------------------------------------
    def _build(self) -> None:
        if self._built:
            return
        m = len(self._raw)
        if m == 0:
            raise ValueError("至少需要一个约束")
        n = self.nvars
        slack_base = n
        art_base = n + m
        rows: list[list[float]] = []
        rhs: list[float] = []
        basis: list[int] = []
        artificial: list[int] = []

        for i, (coefficient, value, sense) in enumerate(self._raw):
            coef = list(coefficient)
            if sense == "<=" and value < 0:
                coef = [-v for v in coef]
                value = -value
                sense = ">="
            elif sense == ">=" and value < 0:
                coef = [-v for v in coef]
                value = -value
                sense = "<="
            elif sense == "=" and value < 0:
                coef = [-v for v in coef]
                value = -value
            if sense == "<=":
                row = coef + [0.0] * m
                row[slack_base + i] = 1.0
                basis.append(slack_base + i)
            elif sense == ">=":
                row = coef + [0.0] * m + [0.0] * (i + 1)
                row[slack_base + i] = -1.0
                row[art_base + i] = 1.0
                basis.append(art_base + i)
                artificial.append(art_base + i)
            else:
                row = coef + [0.0] * m + [0.0] * (i + 1)
                row[art_base + i] = 1.0
                basis.append(art_base + i)
                artificial.append(art_base + i)
            rows.append(row)
            rhs.append(value)

        self.ncols = art_base + m
        for row in rows:
            row.extend([0.0] * (self.ncols - len(row)))
        self.rows = rows
        self.rhs = rhs
        self.basis = basis
        self._artificial = artificial
        forbidden = set(artificial)
        self._structural = [j for j in range(self.ncols) if j not in forbidden]
        self._built = True

    # -- 单纯形内核 ---------------------------------------------------
    def _pivot(self, pivot_row: int, column: int) -> float:
        """以 ``(pivot_row, column)`` 为主元，返回新的检验数行调整系数。"""

        prow = self.rows[pivot_row]
        pivot_value = prow[column]
        if abs(pivot_value) <= EPS:
            raise SolverError("主元为零")
        inverse = 1.0 / pivot_value
        prow[:] = [v * inverse for v in prow]
        self.rhs[pivot_row] *= inverse
        pivot_rhs = self.rhs[pivot_row]
        for i, row in enumerate(self.rows):
            if i == pivot_row:
                continue
            factor = row[column]
            if factor != 0.0:
                row[:] = [a - factor * b for a, b in zip(row, prow)]
                self.rhs[i] -= factor * pivot_rhs
        self.basis[pivot_row] = column
        return inverse

    def _compute_reduced(self, cost: list[float]) -> list[float]:
        basis_costs = [cost[col] for col in self.basis]
        reduced = list(cost)
        for i, bc in enumerate(basis_costs):
            if bc == 0.0:
                continue
            row = self.rows[i]
            for j in range(self.ncols):
                value = row[j]
                if value != 0.0:
                    reduced[j] -= bc * value
        return reduced

    def _objective_value(self, cost: list[float]) -> float:
        return sum(cost[col] * self.rhs[i] for i, col in enumerate(self.basis))

    def _optimize(self, cost: list[float], allowed: list[int]) -> None:
        """在当前基上最大化 ``cost·x``，原地更新基。"""

        allowed_set = set(allowed)
        iteration_limit = 500 + 50 * (len(self.rows) + self.ncols)
        reduced = self._compute_reduced(cost)
        value = self._objective_value(cost)
        stalled = 0
        use_bland = False
        iterations = 0
        while True:
            iterations += 1
            if iterations > iteration_limit:
                raise SolverError("单纯形迭代次数超限，约束系统可能退化")
            candidates = [
                j
                for j in allowed
                if j not in self._blocked and reduced[j] > EPS
            ]
            if not candidates:
                return
            entering = min(candidates) if use_bland else max(
                candidates, key=lambda j: reduced[j]
            )
            leaving = -1
            best = math.inf
            for i, entry_row in enumerate(self.rows):
                coefficient = entry_row[entering]
                if coefficient > EPS:
                    ratio = self.rhs[i] / coefficient
                    if ratio < best - _TIE_TOL:
                        best = ratio
                        leaving = i
                    elif abs(ratio - best) <= _TIE_TOL and leaving >= 0:
                        if self.basis[i] < self.basis[leaving]:
                            leaving = i
            if leaving < 0:
                raise Unbounded("目标无界")
            self._pivot(leaving, entering)
            # 检验数行增量更新：z ← z - z_e·row_r，pivot_row 已是归一化行。
            z_e = reduced[entering]
            prow = self.rows[leaving]
            for j in range(self.ncols):
                adjustment = prow[j]
                if adjustment != 0.0:
                    reduced[j] -= z_e * adjustment
            reduced[entering] = 0.0
            # 目标值直接按当前基重算，数值更稳且开销 O(m)。
            previous = value
            value = self._objective_value(cost)
            if value - previous <= 1e-12:
                stalled += 1
                if stalled >= _STALL_LIMIT:
                    use_bland = True
            else:
                stalled = 0

    def _phase1(self) -> None:
        self._build()
        if self._phase1_done:
            return
        if self._artificial:
            cost = [0.0] * self.ncols
            for col in self._artificial:
                cost[col] = -1.0
            self._optimize(cost, list(range(self.ncols)))
            if self._objective_value(cost) < -_FEAS_TOL:
                raise Infeasible("约束不可行")
            forbidden = set(self._artificial)
            self._blocked.update(forbidden)
            for i in range(len(self.rows)):
                if self.basis[i] in forbidden:
                    for j in self._structural:
                        if abs(self.rows[i][j]) > EPS:
                            self._pivot(i, j)
                            break
        self._phase1_done = True

    # -- 对外求解 -----------------------------------------------------
    def _solution(self, cost: list[float]) -> Solution:
        values = [0.0] * self.nvars
        for i, col in enumerate(self.basis):
            if col < self.nvars:
                values[col] = self.rhs[i]
        values = [0.0 if -_FEAS_TOL < v < 0 else v for v in values]
        return Solution(self._objective_value(cost), tuple(values))

    def maximize(self, objective: list[float]) -> Solution:
        if len(objective) != self.nvars:
            raise ValueError("目标维数不一致")
        self._phase1()
        cost = [float(v) for v in objective] + [0.0] * (self.ncols - self.nvars)
        self._optimize(cost, self._structural)
        return self._solution(cost)

    def clone_with(
        self, extra: list[tuple[list[float], float, str]]
    ) -> "Problem":
        """复制约束并追加若干行，用于反事实比较；不修改原问题。"""

        clone = Problem(self.nvars)
        clone._raw = list(self._raw) + [([float(v) for v in row], float(rhs), sense) for row, rhs, sense in extra]
        return clone

    def minimize(self, objective: list[float]) -> Solution:
        negated = [-float(v) for v in objective]
        result = self.maximize(negated)
        return Solution(-result.value, result.values)

    def feasible(self) -> bool:
        try:
            self._phase1()
        except Infeasible:
            return False
        return True
