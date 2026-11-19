"""应用层错误类型。"""


class FootfallError(Exception):
    """可向用户解释的领域错误。"""


class DataError(FootfallError):
    """输入资料不满足字段或取值口径。"""


class NoExplanation(FootfallError):
    """当前资料下没有任何全部约束相容的解释。"""

    def __init__(self, conflicts, *, hint: str = ""):
        self.conflicts = tuple(conflicts)
        self.hint = hint
        detail = "；".join(self.conflicts) if self.conflicts else "未知原因"
        suffix = f"。{hint}" if hint else ""
        super().__init__(f"没有相容解释：{detail}{suffix}")
