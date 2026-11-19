"""报告：供值班员离线查看的文本与结构化 JSON。

文本报告只陈述约束允许的范围与依据，不给出中点、不把「可能越界」说成
「已经越界」，也不在无相容解释时给出任何限流建议。
"""

from __future__ import annotations


from .advice import Advice, Pressure, advise
from .data import Dataset
from .ledger import Ledger
from .settlement import Settlement, settle
from .solve import SolveResult, solve


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value * 100:.0f}%"


def build_report(
    dataset: Dataset,
    *,
    result: SolveResult | None = None,
    advice: Advice | None = None,
    settlement: Settlement | None = None,
    ledger: Ledger | None = None,
) -> dict:
    result = result or solve(dataset)
    advice = advice or advise(result)
    settlement = settlement or settle(result)
    report: dict = {
        "schema": "footfall-report/1",
        "counter_mode": dataset.policy.counter_mode,
        "topology": {
            "zones": {
                zone: {
                    "capacity": spec.capacity,
                    "door_limit": spec.door_limit,
                    "initial_count": spec.initial_count,
                }
                for zone, spec in sorted(dataset.topology.zones.items())
            },
            "edges": {
                name: {"from": edge.a, "to": edge.b, "max_flow": edge.max_flow}
                for name, edge in sorted(dataset.topology.edges.items())
            },
        },
        "windows": [window.label for window in dataset.windows()],
    }
    if ledger is not None:
        report["basis"] = ledger.basis_summary(ledger.entries)
        report["replay"] = ledger.replay(dataset).describe()

    if result.infeasible:
        report["status"] = "no-explanation"
        report["conflicts"] = list(result.conflicts)
        report["evidence_conflicts"] = result.evidence.conflict_messages()
        report["recommendation"] = (
            "没有相容解释。请现场复点并核对拓扑/容量/关闭登记，"
            "不要修正传感器后放开入口。"
        )
        return report

    report["status"] = "partial" if result.evidence.known_channels() < len(
        result.evidence.channels
    ) else "explained"
    windows = {}
    for window in result.windows():
        total = result.total_range(window)
        zones = {}
        for zone in sorted(result.zones[window]):
            cell = result.zones[window][zone]
            pressure = advice.windows[window][zone]
            zones[zone] = {
                "range": _span_dict(cell.span),
                "capacity": cell.capacity,
                "soft_limit": pressure.soft_limit,
                "pressure": pressure.pressure.value,
                "headroom_worst": pressure.headroom_worst,
                "evidence_sufficient": pressure.evidence_sufficient,
                "conditional_total_max": _span_dict(cell.conditional_total_max),
                "conditional_total_min": _span_dict(cell.conditional_total_min),
                "joint_inconsistent": cell.joint_inconsistent,
            }
        windows[window] = {
            "total": {
                "range": _span_dict(total.span),
                "capacity": total.capacity,
                "pressure": advice.totals[window].pressure.value,
                "headroom_worst": total.headroom_worst,
            },
            "zones": zones,
            "restriction_needed": advice.worst_pressure(window)
            in {Pressure.CAP_POSSIBLE, Pressure.CAP_CERTAIN},
            "levers": [
                {
                    "zone": lever.zone,
                    "total_drop": lever.total_drop,
                    "useful": lever.useful,
                    "is_entrance": lever.is_entrance,
                }
                for lever in advice.levers.get(window, [])
            ],
        }
    report["windows_by_range"] = windows
    report["evidence"] = {
        "registered_channels": sum(1 for c in result.evidence.channels.values() if c.devices),
        "exact_channels": result.evidence.exact_channels(),
        "missing_channels": result.evidence.missing_channels(),
        "duplicates": result.evidence.duplicates,
        "warnings": result.evidence.warnings,
        "conflicts": result.evidence.conflict_messages(),
    }
    report["advice_notes"] = advice.notes
    report["settlement"] = {
        **settlement.summary(),
        "notes": settlement.notes,
        "unsettled": [
            {
                "zone": cell.zone,
                "window": cell.window,
                "range": _span_dict(cell.occupancy),
                "reason": cell.reason,
            }
            for cell in settlement.unsettled
        ],
    }
    return report


def _span_dict(span) -> dict | None:
    if span is None:
        return None
    return {"lo": span.lo, "hi": span.hi, "known": span.hi is not None}


def render_text(report: dict) -> str:
    """把报告字典渲染为可终端查看的文本。"""

    lines: list[str] = []
    push = lines.append
    push("多入口馆区客流可信估计与限量复核")
    push("=" * 36)
    push(f"计数口径：{report.get('counter_mode')}")
    topology = report.get("topology", {})
    if topology:
        zones = "、".join(
            f"{zone}(容量{spec['capacity']}/门限{spec['door_limit']})"
            for zone, spec in topology["zones"].items()
        )
        push(f"馆区：{zones}")
        edges = "、".join(
            f"{name}({spec['from']}↔{spec['to']},≤{spec['max_flow']})"
            for name, spec in topology["edges"].items()
        )
        push(f"连廊：{edges}")
    if report.get("basis"):
        push("")
        push("本次依据")
        for line in report["basis"]:
            push(f"  - {line}")
    if report.get("replay"):
        push("")
        push("当前措施")
        for line in report["replay"]:
            push(f"  - {line}")

    if report.get("status") == "no-explanation":
        push("")
        push("结论：没有相容解释")
        for conflict in report.get("conflicts", []):
            push(f"  ! {conflict}")
        for conflict in report.get("evidence_conflicts", []):
            push(f"  ! {conflict}")
        push(f"建议：{report.get('recommendation')}")
        return "\n".join(lines)

    evidence = report.get("evidence", {})
    push("")
    push("读数归约")
    push(
        f"  已登记通道 {evidence.get('registered_channels', 0)} 个，"
        f"其中精确 {evidence.get('exact_channels', 0)} 个、缺测 {evidence.get('missing_channels', 0)} 个"
    )
    for duplicate in evidence.get("duplicates", []):
        push(f"  - 重复：{duplicate}")
    for warning in evidence.get("warnings", []):
        push(f"  - 注意：{warning}")
    for conflict in evidence.get("conflicts", []):
        push(f"  - 冲突：{conflict}")

    push("")
    push("相容范围（区间不是点估计；?=上界未知）")
    header = "时窗  全馆范围 · 分区范围"
    push(header)
    for window, block in report.get("windows_by_range", {}).items():
        total = block["total"]
        flag = "需限流" if block["restriction_needed"] else ""
        push(
            f"{window:>4}  {_fmt_span(total['range'])} / 容量{total['capacity']} "
            f"[{total['pressure']}] {flag}".rstrip()
        )
        for zone, cell in block["zones"].items():
            soft = f"软线{cell['soft_limit']:.0f}"
            push(
                f"      {zone:<9} {_fmt_span(cell['range']):>12}  容量{cell['capacity']:<3} "
                f"{soft:<7} [{cell['pressure']}]"
            )
            if cell["joint_inconsistent"]:
                push(
                    "        注意：该区单独上界与「全馆取上界」时的上界不同，"
                    "不能同时达到；后者为 "
                    f"{_fmt_span(cell['conditional_total_max'])}"
                )
        levers = block.get("levers") or []
        if levers and any(lever["useful"] for lever in levers):
            best = next(lever for lever in levers if lever["useful"])
            if best.get("is_entrance"):
                push(
                    f"      优先限制入口：{best['zone']}"
                    f"（封顶后全馆最坏值下降 {best['total_drop']} 人）"
                )
            else:
                push(
                    f"      候选限流分区：{best['zone']}"
                    f"（封顶后全馆最坏值下降 {best['total_drop']} 人；"
                    "未登记为入口，仅列候选）"
                )
    for note in report.get("advice_notes", []):
        push(f"  * {note}")

    settlement = report.get("settlement", {})
    push("")
    push("场地小时结算")
    push(
        f"  可结算 {int(settlement.get('settled_cells', 0))} 个分区/时窗，"
        f"{settlement.get('billable_hours_low', 0):.2f}–{settlement.get('billable_hours_high', 0):.2f} 小时"
    )
    for note in settlement.get("notes", []):
        push(f"  * {note}")
    return "\n".join(lines)


def _fmt_span(span: dict | None) -> str:
    if span is None:
        return "—"
    if span["hi"] is None:
        return f"[{span['lo']}, ?]"
    if span["lo"] == span["hi"]:
        return f"[{span['lo']}]"
    return f"[{span['lo']}, {span['hi']}]"
