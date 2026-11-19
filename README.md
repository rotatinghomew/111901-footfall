# 多入口馆区客流可信估计与限量复核

围绕社区全民健身公共服务的本地离线分析。数据均为虚构合成记录；应用只使用
**按馆区/时窗聚合的匿名计数**，不保存个人轨迹，不访问网络，不带任何凭据。

分析包位于 `src/footfall`，输入资料位于 `data/`（CSV + 可选 `policy.json`）。
需要 Python 3.11 或更新版本，**无第三方运行依赖**（线性规划求解器为纯标准库实现）。

## 运行

```bash
# 文本报告（默认读取 data/policy.json，若存在）
python -m footfall --data data            # 需 PYTHONPATH=src
PYTHONPATH=src python -m footfall --data data

# 结构化 JSON / 写入文件
PYTHONPATH=src python -m footfall --data data --json --out var/report.json

# 测试（无需设置 PYTHONPATH）
python -m unittest discover -s tests -v
```

也可作为库使用：

```python
from footfall import Dataset, build_evidence, solve, advise, settle, build_report
dataset = Dataset.from_dir("data")
result = solve(dataset)                  # 相容范围与极值
advice = advise(result)                  # 软警戒线 / 入口优先级
report = build_report(dataset, result=result, advice=advice)
```

## 问题模型

### 拓扑与容量

- 每个馆区有 `capacity`（同时容纳上限）、`door_limit`（每窗口、每方向门总吞吐）、
  `initial_count`（开班人数）。
- 连廊无向，`max_flow` 是**双向合计**的每窗口同时通过上限。
- 每个窗口每个馆区守恒：在场变化 = 外部进出 + 各连廊净流入。跨区流动用同一个
  流量变量表示（在 A 记离开、在 B 记进入），因此**全馆净人数只能由外部进出改变**，
  不会凭空增长。
- `in`/`out` 读数是各馆区自己闸机/门上的外部进出（多入口馆区每个区都可能有门）；
  `cross` 读数是在相邻两区各计一次的跨区流动，约束该区相邻连廊双向合计。
- 政策 `entrance_zones` 只用于把某一馆区登记为「仅此一处有外部入口」；登记后其余
  馆区的外部进出被固定为 0。未登记时不施加此假设，避免把「不知道门在哪」当成
  「没有门」。

### 读数归约（`footfall.evidence`）

不对累计读数取最近值、不把缺测/重置统一填零。每种状态都保留为证据：

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| 正计数正常 | `cur - prev` | 精确值 |
| 倒计数正常 | `prev - cur` | 精确值 |
| 重置/回绕 | `reset=1` 或补来的重启证明 | 正计数给 `[0, cur]`；倒计数给精确回绕人次 |
| 缺测 | 中间窗口无读数 | 不生成 0；跨窗总量给范围，单窗上界未知 |
| 异常 | 读数下降但未登记重置等 | 保留宽区间并标注，不静默修正 |
| 双机重复 | 同设备同窗口多行 | 只计一次；不一致时标注冲突，不叠加 |

多设备按政策 `evidence_aggregation` 处理：`overlap`（默认，同一通道冗余回报，取包络，
绝不相加）或 `disjoint`（不同门，求和）。

计数口径由政策 `counter_mode`（`cumulative` / `countdown`）声明，不由程序猜测；
首个窗口可通过 `initial_counter_value` 结算，否则为未知。

### 相容范围（`footfall.solve`）

输出的是**约束允许的区间，不是点估计**，也从不输出区间中点。每个区、每个窗口和
全馆都有 `[下界, 上界]`；上界未知时显示 `?`，不拿 0 或最近值顶替。

- 各区单独可达上界之和可能大于全馆上界。报告额外给出「全馆取上界/下界时该区
  还能有多少」（字典序目标、同一解释），并标注 `joint_inconsistent`，不把两个
  不同解释的极值拼成一个实际人数。
- 无相容解释时返回 `status: no-explanation`、冲突清单与**现场复点**建议；
  **不**修正传感器，**不**放开入口。

### 限量措施与冲突读数

- 已确认的关闭通道自登记窗口起生效（`closure_persistence: forward`），该连廊
  双向流量为 0。
- 若后续读数只能来自已关闭通道（跨区读数而相邻连廊全关、或非入口馆区在连廊
  全关时仍有出入、或闭馆时段仍有流动），**撤下该读数、保留限制、标注冲突**，
  不把它当作放宽限制的证据。`conflict_policy: stop` 会同时提示停止给出范围。

### 软警戒线 vs 容量

`soft_margin × capacity` 是关注线；`capacity` 是必须限流的硬线。每个分区/时窗
分五档报告：`ok`、`soft-possible`、`soft-certain`、`cap-possible`、`cap-certain`，
另有 `unknown`（证据不足）。「最坏合理状态可能超限」与「最有利状态也已超限」
分开陈述。

先限制哪个入口由**反事实**给出：把某候选入口在该窗口封顶为 0，重新求全馆最坏
在场人数的下降量；按下降排序，且只把政策登记为入口的分区（`entrance_zones`）
称作入口；未登记时只在报告中列出候选与下降量，不臆断哪一个是「入口」。
同时报告哪些分区证据不足、哪些仍有局部余量可承接。

### 场地小时结算（`footfall.settlement`）

- 只有证据充分的分区/时窗进入结算：至少一条已登记通道证据、区间宽度不超过
  `max_settle_uncertainty`（若设置）、上界已知。
- 结算量是区间：`场地小时 ∈ [时长×lo/容量, 时长×hi/容量]`，不取中点。
- 缺测、读数撤下、区间过宽的时窗单列入 `unsettled` 并说明原因。

### 档案与追查（`footfall.ledger`）

- 追加式 JSONL，每条记录带序号与 SHA-256 摘要；被改写会立即报错。
- 记录补来的重启证明、人工点数、关闭通道、入口限量（含**当时依据**）与分析快照。
- 补证据产生新版本，旧记录及其依据保留不变；`replay()` 还原当前资料与措施。
- `query(window=..., zone=...)` 按入口/时窗追查，字段只有馆区、时窗、聚合人数，
  无个人级数据。档案默认放 `var/`（已被 `.gitignore` 忽略），只在本地查看。

## 数据文件

| 文件 | 字段 |
| --- | --- |
| `zones.csv` | `zone, capacity, door_limit, initial_count` |
| `edges.csv` | `edge, from, to, max_flow` |
| `readings.csv` | `device, zone, window, cumulative, reset, kind`（`kind∈{in,out,cross}`） |
| `incidents.csv` | `window, zone, manual_count, closed_edges, confirmed` |
| `policy.json` | 见下 |

空字段表示未登记，不做推断。`confirmed=0` 的人工点数只登记，不作为硬证据。

### policy.json

```json
{
  "counter_mode": "countdown",
  "counter_ceiling": 60,
  "initial_counter_value": null,
  "soft_margin": 0.85,
  "window_minutes": 15,
  "day_start": "08:00",
  "open_from": "", "open_until": "",
  "opening_windows": [],
  "entrance_zones": [],
  "closure_persistence": "forward",
  "evidence_aggregation": "overlap",
  "conflict_policy": "flag",
  "max_settle_uncertainty": 40
}
```

未登记开闭时段时全部窗口按开放处理；`opening_windows` 可显式列出开放窗口。

## 测试与资料检查

`tests/` 覆盖：区间外向取整、正/倒计数归约、重置与缺测不填零、双机重复、
关闭通道冲突、人工点数真伪、守恒与容量、极值不可同时达到、软线/容量区分、
结算门槛、档案防篡改与按入口/时窗追查、报告不含中点与无解释时的行为。
