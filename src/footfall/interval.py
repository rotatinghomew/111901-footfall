"""区间证据类型。

所有人数按整数处理。区间只表示约束允许的取值集合，不附带分布假设，也不
提供「代表值」：调用方不得用中点替代原始证据。

``hi=None`` 表示该方向没有登记上界（不是「无限多人」的业务判断，而是
「现有资料不足以给出上界」）。空区间用 ``None`` 表示不可行，不用异常。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """闭区间 ``[lo, hi]``；``hi=None`` 表示上界未知。"""

    lo: int
    hi: int | None

    def __post_init__(self):
        if not isinstance(self.lo, int):
            raise TypeError("区间下界必须是整数")
        if self.hi is not None:
            if not isinstance(self.hi, int):
                raise TypeError("区间上界必须是整数或 None")
            if self.lo > self.hi:
                raise ValueError(f"空区间 [{self.lo}, {self.hi}] 不能作为约束结果")

    # -- 构造 ---------------------------------------------------------
    @classmethod
    def point(cls, value: int) -> "Span":
        return cls(value, value)

    @classmethod
    def nonnegative(cls, hi: int) -> "Span":
        return cls(0, hi)

    @classmethod
    def unknown(cls) -> "Span":
        return cls(0, None)

    # -- 属性 ---------------------------------------------------------
    @property
    def is_point(self) -> bool:
        return self.hi is not None and self.lo == self.hi

    @property
    def is_bounded(self) -> bool:
        return self.hi is not None

    @property
    def width(self) -> int | None:
        """可行取值个数；无上界时为 ``None``。"""
        if self.hi is None:
            return None
        return self.hi - self.lo + 1

    @property
    def uncertainty(self) -> int | None:
        """证据宽度 ``hi - lo``；点值时为 0。"""
        if self.hi is None:
            return None
        return self.hi - self.lo

    # -- 运算 ---------------------------------------------------------
    def intersect(self, other: "Span") -> "Span | None":
        lo = max(self.lo, other.lo)
        if self.hi is None:
            hi = other.hi
        elif other.hi is None:
            hi = self.hi
        else:
            hi = min(self.hi, other.hi)
        if hi is not None and lo > hi:
            return None
        return Span(lo, hi)

    def union(self, other: "Span") -> "Span":
        """两个证据的包络；不表示二者都成立。"""
        lo = min(self.lo, other.lo)
        if self.hi is None or other.hi is None:
            return Span(lo, None)
        return Span(lo, max(self.hi, other.hi))

    def add(self, other: "Span") -> "Span":
        hi = None if self.hi is None or other.hi is None else self.hi + other.hi
        return Span(self.lo + other.lo, hi)

    def shift(self, amount: int) -> "Span":
        hi = None if self.hi is None else self.hi + amount
        return Span(self.lo + amount, hi)

    def contains(self, value: int) -> bool:
        return self.lo <= value and (self.hi is None or value <= self.hi)

    def __str__(self) -> str:
        if self.hi is None:
            return f"[{self.lo}, ?]"
        if self.lo == self.hi:
            return f"[{self.lo}]"
        return f"[{self.lo}, {self.hi}]"


def intersect_all(spans: list[Span]) -> Span | None:
    result = Span.unknown()
    for span in spans:
        merged = result.intersect(span)
        if merged is None:
            return None
        result = merged
    return result


def sum_all(spans: list[Span]) -> Span:
    lo = 0
    hi: int | None = 0
    for span in spans:
        lo += span.lo
        hi = None if hi is None or span.hi is None else hi + span.hi
    return Span(lo, hi)


def outward_round(value: float, *, lower: bool) -> int:
    """把 LP 端点按整数口径向外取整，避免把分数当成实际人数。"""

    rounded = int(value)
    if lower:
        # 下界向上取整（允许浮点误差）。
        if value - rounded > 1e-9:
            rounded += 1
        return rounded
    # 上界向下取整。
    if rounded - value > 1e-9:
        rounded -= 1
    return rounded
