"""领域应用包：多入口馆区客流可信估计与限量复核。

模块划分：

- :mod:`footfall.interval`：区间证据类型 ``Span``；
- :mod:`footfall.data`：CSV/JSON 载入与运行口径；
- :mod:`footfall.evidence`：累计读数归约（重置、重复、缺测、人工点数）；
- :mod:`footfall.model`：拓扑、容量与流动约束；
- :mod:`footfall.solve`：相容范围、极值与不可行诊断；
- :mod:`footfall.advice`：软警戒线、入口优先级与证据充分性；
- :mod:`footfall.settlement`：按利用率结算的场地小时；
- :mod:`footfall.ledger`：追加式证据/决策档案与争议追查；
- :mod:`footfall.report`：文本与 JSON 报告；
- :mod:`footfall.cli`：本地离线命令行。

应用不访问网络，不保存个人轨迹：所有资料都是按馆区/时窗聚合的匿名计数。
"""

from .advice import Advice, EntranceLever, Pressure, ZonePressure, advise
from .data import Dataset, Edge, Incident, Policy, Reading, Topology, Window, Zone
from .errors import DataError, FootfallError, NoExplanation
from .evidence import ChannelEvidence, EvidenceBundle, RestartProof, build_evidence
from .interval import Span
from .ledger import Ledger, Replay
from .model import EdgeClosure, EntryCap, FootfallModel, ModelDiagnostics
from .report import build_report, render_text
from .settlement import Settlement, settle
from .solve import SolveResult, TotalRange, ZoneWindowRange, solve

__all__ = [
    "Advice",
    "ChannelEvidence",
    "Dataset",
    "DataError",
    "Edge",
    "EdgeClosure",
    "EntranceLever",
    "EntryCap",
    "EvidenceBundle",
    "FootfallError",
    "FootfallModel",
    "Incident",
    "Ledger",
    "ModelDiagnostics",
    "NoExplanation",
    "Policy",
    "Pressure",
    "Reading",
    "Replay",
    "RestartProof",
    "Settlement",
    "SolveResult",
    "Span",
    "Topology",
    "TotalRange",
    "Window",
    "Zone",
    "ZonePressure",
    "ZoneWindowRange",
    "advise",
    "build_evidence",
    "build_report",
    "render_text",
    "settle",
    "solve",
]

__version__ = "1.0.0"
