"""追加式证据/决策档案：可复核、可争议追查、只在本机。

设计
====

- 一个 :class:`Ledger` 对应一个本地 JSONL 文件；每条记录只追加，带
  ``seq`` 与内容摘要（SHA-256），可校验未被改写。
- 记录分三类：
  * ``evidence``：补来的重启证明、补来的人工点数、登记的关闭通道；
  * ``restriction``：值班员登记的入口限量/关闭措施，以及**当时依据了哪些资料**
    （``basis`` 摘要与 ``assumption`` 文字）；
  * ``analysis``：某次分析输出的范围快照与结算口径，便于事后对照。
- :meth:`replay` 把档案还原成当前 :class:`Dataset` 与入口限制，**不修改**
  已写入的历史；后补证据产生新版本，旧结论仍保留「当时依据」。
- :meth:`query` 按入口/时窗返回该时窗内所有相关记录与资料版本，不涉及
  任何个人轨迹：档案字段只有馆区、时窗、聚合人数与政策标识。

离线约束：只用标准库，不做网络访问；档案路径由调用方给出，默认放在
项目 ``var/`` 下且被 ``.gitignore`` 排除。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .data import Dataset, Incident, window_index
from .errors import DataError
from .evidence import RestartProof
from .model import EntryCap

SCHEMA = "footfall-ledger/1"


class LedgerError(DataError):
    pass


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    recorded_at: str
    payload: dict
    digest: str

    def as_row(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "payload": self.payload,
            "digest": self.digest,
        }


@dataclass
class Replay:
    dataset: Dataset
    entry_caps: tuple[EntryCap, ...]
    restart_proofs: tuple[RestartProof, ...]
    removals: tuple[tuple[str, str], ...]
    origin: str = ""

    def describe(self) -> list[str]:
        lines = []
        for cap in self.entry_caps:
            lines.append(f"入口限制：{cap.zone} 自 {cap.start_window} 起每窗至多 {cap.cap} 人")
        for proof in self.restart_proofs:
            lines.append(f"重启证明：设备 {proof.device} 在 {proof.window}")
        for zone, window in self.removals:
            lines.append(f"读数撤下：{zone} 在 {window} 的人工点数不作为硬证据")
        return lines


class Ledger:
    """追加式档案。``path`` 为 ``None`` 时只在内存中保存。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.entries: list[Entry] = []
        if self.path is not None and self.path.exists():
            self._load()

    # -- 读写 ---------------------------------------------------------
    def _load(self) -> None:
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{self.path.name} 第 {lineno} 行不是合法 JSON") from exc
            payload = row.get("payload", {})
            expected = _digest(payload)
            if row.get("digest") != expected:
                raise LedgerError(f"{self.path.name} 第 {lineno} 行摘要不符，档案可能被改写")
            entry = Entry(
                seq=int(row["seq"]),
                kind=str(row["kind"]),
                recorded_at=str(row.get("recorded_at", "")),
                payload=payload,
                digest=str(row["digest"]),
            )
            if entry.seq != len(self.entries):
                raise LedgerError(f"{self.path.name} 第 {lineno} 行序号不连续")
            self.entries.append(entry)

    def append(
        self,
        kind: str,
        payload: dict,
        *,
        recorded_at: str | None = None,
    ) -> Entry:
        if kind not in {"evidence", "restriction", "analysis"}:
            raise LedgerError(f"未知档案类型：{kind}")
        digest = _digest(payload)
        entry = Entry(
            seq=len(self.entries),
            kind=kind,
            recorded_at=recorded_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload=payload,
            digest=digest,
        )
        self.entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry.as_row(), ensure_ascii=False) + "\n")
        return entry

    # -- 登记便利方法 -------------------------------------------------
    def record_restart_proof(
        self, device: str, window: str, *, recorded_at: str | None = None, note: str = ""
    ) -> Entry:
        window_index(window)
        return self.append(
            "evidence",
            {"type": "restart-proof", "device": device, "window": window, "note": note},
            recorded_at=recorded_at,
        )

    def record_manual_count(
        self,
        zone: str,
        window: str,
        count: int,
        *,
        confirmed: bool,
        recorded_at: str | None = None,
        note: str = "",
    ) -> Entry:
        window_index(window)
        if count < 0:
            raise LedgerError("人工点数不能为负")
        return self.append(
            "evidence",
            {
                "type": "manual-count",
                "zone": zone,
                "window": window,
                "count": count,
                "confirmed": bool(confirmed),
                "note": note,
            },
            recorded_at=recorded_at,
        )

    def record_closure(
        self,
        edge: str,
        start_window: str,
        *,
        recorded_at: str | None = None,
        note: str = "",
    ) -> Entry:
        window_index(start_window)
        return self.append(
            "restriction",
            {
                "type": "closure",
                "edge": edge,
                "start_window": start_window,
                "note": note,
            },
            recorded_at=recorded_at,
        )

    def record_entry_cap(
        self,
        zone: str,
        start_window: str,
        cap: int,
        *,
        basis: Iterable[str] = (),
        recorded_at: str | None = None,
        note: str = "",
    ) -> Entry:
        if cap < 0:
            raise LedgerError("入口限量不能为负")
        window_index(start_window)
        return self.append(
            "restriction",
            {
                "type": "entry-cap",
                "zone": zone,
                "start_window": start_window,
                "cap": cap,
                "basis": list(basis),
                "note": note,
            },
            recorded_at=recorded_at,
        )

    def record_analysis(
        self,
        *,
        window_ranges: dict,
        settlement: dict | None = None,
        basis: Iterable[str] = (),
        recorded_at: str | None = None,
    ) -> Entry:
        return self.append(
            "analysis",
            {
                "type": "analysis",
                "window_ranges": window_ranges,
                "settlement": settlement or {},
                "basis": list(basis),
            },
            recorded_at=recorded_at,
        )

    # -- 回放 ---------------------------------------------------------
    def replay(self, dataset: Dataset) -> Replay:
        """把档案应用到 ``dataset``，得到当前资料与措施。"""

        readings = list(dataset.readings)
        incidents = list(dataset.incidents)
        caps: list[EntryCap] = []
        proofs: list[RestartProof] = []
        removals: list[tuple[str, str]] = []
        seen_proofs: set[tuple[str, str]] = set()

        occurred = [(e.recorded_at, e.seq, e) for e in self.entries]
        # 序号优先：同一秒内的多次登记按追加顺序解释。
        occurred.sort(key=lambda item: (item[0], item[1]))

        for _, _, entry in occurred:
            payload = entry.payload
            kind = payload.get("type")
            if entry.kind == "evidence" and kind == "restart-proof":
                key = (payload["device"], payload["window"])
                if key not in seen_proofs:
                    seen_proofs.add(key)
                    proofs.append(RestartProof(**{"device": key[0], "window": key[1]}))
            elif entry.kind == "evidence" and kind == "manual-count":
                incidents.append(
                    Incident(
                        window=payload["window"],
                        zone=payload["zone"],
                        manual_count=int(payload["count"]),
                        closed_edges=(),
                        confirmed=bool(payload["confirmed"]),
                    )
                )
                if not payload["confirmed"]:
                    removals.append((payload["zone"], payload["window"]))
            elif entry.kind == "restriction" and kind == "closure":
                incidents.append(
                    Incident(
                        window=payload["start_window"],
                        zone=_zone_of_edge(dataset, payload["edge"]),
                        manual_count=None,
                        closed_edges=(payload["edge"],),
                        confirmed=True,
                    )
                )
            elif entry.kind == "restriction" and kind == "entry-cap":
                caps.append(
                    EntryCap(
                        zone=payload["zone"],
                        start_window=payload["start_window"],
                        cap=int(payload["cap"]),
                        label="档案登记",
                    )
                )
            elif entry.kind in {"analysis"}:
                continue
            else:
                raise LedgerError(f"无法解释的档案记录：{entry.seq}")

        replay_dataset = dataset.replace(readings=readings, incidents=incidents)
        return Replay(replay_dataset, tuple(caps), tuple(proofs), tuple(removals))

    # -- 追查 ---------------------------------------------------------
    def query(
        self,
        *,
        window: str | None = None,
        zone: str | None = None,
        kind: str | None = None,
    ) -> list[Entry]:
        """按入口/时窗追查相关记录；不含任何个人级字段。"""

        matches = []
        for entry in self.entries:
            if kind is not None and entry.kind != kind:
                continue
            payload = entry.payload
            entry_windows = {
                value
                for key, value in payload.items()
                if key in {"window", "start_window"} and isinstance(value, str)
            }
            if "window_ranges" in payload:
                entry_windows.update(payload["window_ranges"].keys())
            entry_zones = {
                value
                for key, value in payload.items()
                if key == "zone" and isinstance(value, str)
            }
            if "window_ranges" in payload:
                for ranges in payload["window_ranges"].values():
                    if isinstance(ranges, dict):
                        entry_zones.update(ranges.keys())
            if window is not None and window not in entry_windows:
                continue
            if zone is not None and zone not in entry_zones:
                continue
            matches.append(entry)
        return matches

    def basis_summary(self, entries: Iterable[Entry]) -> list[str]:
        """把记录转成人类可读的依据说明。"""

        lines = []
        for entry in entries:
            payload = entry.payload
            kind = payload.get("type", entry.kind)
            if kind == "restart-proof":
                lines.append(
                    f"重启证明：{payload['device']} 在 {payload['window']}"
                    f"（{entry.recorded_at}）"
                )
            elif kind == "manual-count":
                status = "已确认" if payload["confirmed"] else "未确认"
                lines.append(
                    f"人工点数：{payload['window']}/{payload['zone']} = {payload['count']}"
                    f"（{status}，{entry.recorded_at}）"
                )
            elif kind == "closure":
                lines.append(
                    f"关闭通道：{payload['edge']} 自 {payload['start_window']}"
                    f"（{entry.recorded_at}）"
                )
            elif kind == "entry-cap":
                basis = payload.get("basis") or []
                tail = f"，依据 {len(basis)} 条" if basis else ""
                lines.append(
                    f"入口限量：{payload['zone']} 自 {payload['start_window']} 起 "
                    f"每窗至多 {payload['cap']} 人{tail}（{entry.recorded_at}）"
                )
            elif kind == "analysis":
                lines.append(f"分析快照（{entry.recorded_at}）")
        return lines


def _zone_of_edge(dataset: Dataset, edge: str) -> str:
    if edge not in dataset.topology.edges:
        raise LedgerError(f"关闭登记引用未登记连廊 {edge}")
    return dataset.topology.edges[edge].a


def open_ledger(path: Path | None) -> Ledger:
    return Ledger(path)
