"""本地离线命令行。

用法::

    python -m footfall --data data [--policy data/policy.json] [--json] [--out FILE]
    python -m footfall --data data --ledger var/ledger.jsonl --query w4 --zone lobby
    python -m footfall --data data --ledger var/ledger.jsonl \\
        --record-entry-cap lobby w4 0 --basis "w3 人工点数 6"

不访问任何网络；所有结果只在本机打印或写入指定文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .advice import advise
from .data import Dataset, Policy
from .errors import FootfallError
from .ledger import Ledger, LedgerError
from .report import build_report, render_text
from .settlement import settle
from .solve import solve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="footfall",
        description="多入口馆区客流可信估计与限量复核（本地离线）",
    )
    parser.add_argument("--data", type=Path, default=Path("data"), help="CSV 资料目录")
    parser.add_argument("--policy", type=Path, help="政策 JSON；缺省尝试 <data>/policy.json")
    parser.add_argument("--ledger", type=Path, help="追加式档案（JSONL）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--out", type=Path, help="把报告写入文件而不是标准输出")
    parser.add_argument("--no-levers", action="store_true", help="跳过入口反事实探测（更快）")

    record = parser.add_argument_group("登记（写入档案）")
    record.add_argument("--record-restart-proof", nargs=2, metavar=("DEVICE", "WINDOW"))
    record.add_argument("--record-manual-count", nargs=3, metavar=("ZONE", "WINDOW", "COUNT"))
    record.add_argument("--record-closure", nargs=2, metavar=("EDGE", "START_WINDOW"))
    record.add_argument("--record-entry-cap", nargs=3, metavar=("ZONE", "START_WINDOW", "CAP"))
    record.add_argument("--basis", action="append", default=[], help="登记限量时的依据说明")

    query = parser.add_argument_group("追查")
    query.add_argument("--query", metavar="WINDOW", help="列出该时窗相关档案记录")
    query.add_argument("--zone", help="与 --query 联用，按馆区过滤")
    return parser


def _load_policy(args) -> Policy:
    if args.policy is not None:
        return Policy.load(args.policy)
    candidate = args.data / "policy.json"
    if candidate.exists():
        return Policy.load(candidate)
    return Policy()


def _record(args, ledger: Ledger) -> None:
    if args.record_restart_proof:
        device, window = args.record_restart_proof
        ledger.record_restart_proof(device, window, note="命令行登记")
    if args.record_manual_count:
        zone, window, count = args.record_manual_count
        ledger.record_manual_count(
            zone, window, int(count), confirmed=True, note="命令行登记"
        )
    if args.record_closure:
        edge, start = args.record_closure
        ledger.record_closure(edge, start, note="命令行登记")
    if args.record_entry_cap:
        zone, start, cap = args.record_entry_cap
        ledger.record_entry_cap(zone, start, int(cap), basis=args.basis, note="命令行登记")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = _load_policy(args)
        dataset = Dataset.from_dir(args.data, policy)
        ledger = Ledger(args.ledger) if args.ledger else None

        if ledger is not None and (
            args.record_restart_proof
            or args.record_manual_count
            or args.record_closure
            or args.record_entry_cap
        ):
            _record(args, ledger)

        if args.query:
            if ledger is None:
                raise FootfallError("追查档案需要 --ledger")
            entries = ledger.query(window=args.query, zone=args.zone)
            for line in ledger.basis_summary(entries):
                print(line)
            return 0

        caps = ()
        proofs = ()
        if ledger is not None:
            replay = ledger.replay(dataset)
            dataset = replay.dataset
            caps = replay.entry_caps
            proofs = replay.restart_proofs
            for line in replay.describe():
                print(line)

        from .evidence import build_evidence

        evidence = build_evidence(dataset, restart_proofs=proofs)
        result = solve(dataset, evidence=evidence, entry_caps=caps)
        advice = advise(result, probe_levers=not args.no_levers)
        settlement = settle(result)
        report = build_report(
            dataset,
            result=result,
            advice=advice,
            settlement=settlement,
            ledger=ledger if ledger is not None else None,
        )
        if ledger is not None and not result.infeasible:
            ledger.record_analysis(
                window_ranges={
                    window: {
                        zone: {"lo": cell.span.lo, "hi": cell.span.hi}
                        for zone, cell in result.zones[window].items()
                    }
                    for window in result.windows()
                },
                settlement=settlement.summary(),
            )

        text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    except (FootfallError, LedgerError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
