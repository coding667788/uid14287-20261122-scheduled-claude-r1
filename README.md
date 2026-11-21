# 数字取证：分区镜像的一致切片与污染判定

对**完全虚构**的介质建模：区块、采集可信标记、区块摘要、写入事务与镜像分片
全部来自输入 JSON，程序不读取任何真实磁盘、分区或个人数据，仅使用 Python
标准库与 CPU。

- 运行环境：Windows 原生 Python 3.12（任何 3.10+ 亦可），无第三方依赖。
- 命令形式：`python -m solution <命令> CASE.json`
- 结果：确定性的 UTF-8 JSON 写到标准输出；结构错误以退出码 2 写到标准错误。

## 目录

```
solution/            生产代码（纯标准库）
  model.py           解析、结构校验、偏序(DAG)与同块写可比性检查
  horn.py            Horn 子句与最小模型（前向链）引擎
  engine.py          证据→Horn 公式编译、切片求解、区块状态
  reconstruct.py     一致切片判定、极小冲突集
  compare.py         候选切片核查、逐块可能性、联合最大差异
  suspect.py         最小成本隔离、冲突核引导分支限界
  cli.py / __main__.py
examples/            虚构可运行样例
tests/               python -m unittest discover -s tests -v
```

## 复现命令

```bash
# 三个命令分别运行
python -m solution reconstruct examples/case_reconstruct.json
python -m solution reconstruct examples/case_conflict.json
python -m solution compare     examples/case_compare.json
python -m solution suspect     examples/case_suspect.json

# 全部断言测试
python -m unittest discover -s tests -v
```

也可用 `-` 代替文件路径，从标准输入读取 JSON。

## 建模与语义

### 区块与事务

- 每个区块有 `initial_digest`（与可选 `initial_source`）。
- 事务含采集器、本地日志序号 `local_seq`、跨采集器因果先决 `causal_after`、
  原子写入集合 `writes`（列出全部被改区块及改后摘要）。
- 同一采集器的事务按 `local_seq` 排成全序；跨采集器因果由输入声明。两者取并集
  构成偏序 DAG，环、重复本地序号均为结构错误。
- **同块写可比性（结构约束）**：任意两个写同一区块的事务必须能由偏序排序，
  否则报 `E_WRITE_INCOMPARABLE`。因此每个区块的写者构成一条全序链。
- 一次事务的所有区块**同时可见**（原子性）；纳入某事务必须纳入其全部先决
  （切片是偏序理想 / 下闭包）。

### 切片（取证时刻）

切片是满足偏序的全局前缀：一个事务变量集合，沿因果边向下封闭。区块在切片中的
状态由其写者链上“最后一个被纳入的写事务”决定；没有写者纳入时取初始来源。
**摘要被改回原值时，最后写入者仍是最后一个纳入的写事务**，不会退化为初始来源。

### 镜像分片

每个分片声明：所属采集器、块域观察（每块一个摘要）、证据屏障
（`must_include` / `must_exclude`）。未采集的区块不施加任何摘要约束。
全部分片必须能由**同一个**切片满足；禁止把各分片各自的可行时刻拼成整盘证据。

## 输入 JSON 定义

顶层字段：

| 字段 | 说明 |
|---|---|
| `case_id`, `media_id` | 字符串标识，缺省有确定性默认值 |
| `blocks[]` | `id`、`initial_digest`、可选 `initial_source` |
| `collectors[]` | `id`、可选 `trust_marker` |
| `transactions[]` | 见下 |
| `shards[]` | 见下 |
| `compare` | 可选，compare 命令配置 |
| `suspect` | 可选，suspect 命令配置 |

事务对象：

```json
{"id": "T1", "collector": "C1", "local_seq": 0,
 "causal_after": [], "actor": "ACT-ALPHA",
 "writes": [{"block": "B0", "digest": "h0-a"}]}
```

分片对象：

```json
{"id": "S1", "collector": "C1", "revocable": true,
 "barriers": {"must_include": ["T1"], "must_exclude": []},
 "observations": [{"block": "B0", "digest": "h0-a"}]}
```

`compare`：

```json
{"witness_blocks": ["BX", "BY"],
 "slices": [["T1"], ["T1", "T2"]],
 "preservation": {"BX": "x0", "BY": "y0"}}
```

`suspect`：

```json
{"isolation_costs": {"C1": 5, "C2": 1},
 "fixed_evidence": ["SF"]}
```

拥有可撤分片的采集器必须出现在 `isolation_costs`；`fixed_evidence` 只能引用
`revocable=false` 的分片。

## 输出 JSON 定义（摘要）

- `reconstruct`：`feasible`；可行时给 `slice{transaction_count, transaction_ids}`
  与每个已采集块的 `block_states`（含 `source_kind`、`source`、`writer_txn`、
  `collector`、`digest`）；不可行时给 `conflict_core`、确定性 `removal_trace`、
  `conflict_detail`（子句溯源）。
- `compare`：两个候选切片的理想性/证据满足性与逐块状态；每块
  `block_possibilities`（每个可能前缀的来源与摘要）；`naive_per_block_mismatch_sum`
  与 `joint_max_mismatch`（同一个可行切片下的联合最大差异、对应切片与逐块标志）。
- `suspect`：`optimal_isolation{isolated_collectors,total_cost,collector_count}`、
  `remaining_slice`、`active_shards`、`shard_explanations`、
  `exhaustive_check`（小规模独立穷举核对结果）。

## 核心算法：为什么全部是 Horn 子句

为每个事务设布尔变量 x_i（纳入切片）。

- DAG 边 u 先于 v：`x_v ⇒ x_u`（确定 Horn 子句）。
- 屏障 `must_include T`：单位正子句 `x_T`；`must_exclude T`：目标 `¬x_T`。
- 区块摘要观察：区块写者链 w1<…<wk，切片只可能取某个前缀 p（0..k）。对观察
  摘要不允许的前缀：
  - 禁止 p=0：单位正 `x_w1`（至少一个写者纳入）；
  - 禁止 0<p<k：确定子句 `(x_w1∧…∧x_wp) ⇒ x_w{p+1}`（强制前缀越过 p）；
  - 禁止 p=k：目标 `¬(x_w1∧…∧x_wk)`；
  - 没有任何前缀产生该摘要：恒假目标。

所有子句至多含一个正字面量，因此：

1. 可满足性由 BFS 前向链在线性时间判定；
2. 可行模型集合对**交、并封闭**。事务数最少的切片是从单位正子句出发的唯一
   最小模型——所以“同数取字典序最小 ID 列表”的决胜自动满足（最小者唯一）。

### 极小冲突集（reconstruct 无解时）

事务日志与偏序始终保留，只对分片操作。按分片 ID **降序**逐个尝试移除：
移除后仍无解则永久移除，移除后有解则恢复。最终保留者构成一个包含极小
（inclusion-minimal）冲突集，全过程在 `removal_trace` 中可复现。

### compare：为什么不能把逐块差异直接相加

逐块“可出现的来源/摘要”是在**不同**切片上分别枚举的；而真实取证只有一个
切片，各块由同一组事务变量耦合（原子多区块事务、因果边、共享写者）。
联合最大差异在**同一个证据可行切片**上做 DPLL 搜索：决策变量取见证块的写者
链前缀，每步加确定/目标 Horn 约束并立即用最小模型传播与冲突剪枝，带记忆化，
按“差异数→事务数→ID 字典序”决胜。输出始终是单个自洽切片。

> 理论性质：在纯 Horn 证据下，模型族对并封闭，且“摘要不匹配”是前缀的向上闭包
> 性质，因此“逐块朴素和”与“联合最大”在数值上相等；但二者语义不同——联合值
> 必须由一个真实可行切片实现。当证据把某些块锁在匹配前缀（见
> `examples/case_compare.json`）时，逐块可能性仍列出差异状态（朴素和=2），
> 而联合最大差异为 0，直观展示了不能跨切片拼凑。候选切片本身也可能不是证据
> 模型，报告中分别给出 `is_ideal` 与 `satisfies_evidence`。

### suspect：搜索共享与剪枝如何保持最优性

可行性关于“保留分片集合”单调（移除分片只会把不可行变可行）。搜索为冲突核
引导的分支限界：

1. **冲突核**：对当前分片集合用与 reconstruct 相同的降序贪心求包含极小冲突核，
   结果缓存；
2. **完备剪枝**：任何可行隔离都必须切断当前冲突核，因此只需在“拥有冲突核中
   可撤分片”的采集器上分支；冲突核全为固定证据时该分支立即判死；
3. **界剪枝**：累计成本严格超过已知最优才剪（同成本不剪，因为零代价隔离不
   增成本，仍可能得到更少台数/更优字典序）；
4. **搜索共享**：Horn 求解与冲突核都以分片集合为键缓存，跨分支复用；
5. **独立核对**：可隔离采集器 ≤ 12 时，另用完全独立的子集穷举
   （`itertools.combinations`）求最优并断言一致；> 12 时跳过穷举，靠剪枝与
   缓存保证规模。测试中对 500 个随机小案例做了穷举交叉验证。

该过程不枚举事务的完整排列（80! 不可行），只在 Horn 最小模型与分片子集上工作，
80 事务 / 30 分片 / 16 采集器在普通笔记本上秒级完成。

## 错误码

| 码 | 含义 |
|---|---|
| `E_JSON` / `E_FILE` | JSON 解析失败 / 文件不存在 |
| `E_TYPE` | 字段类型错误（含 `local_seq` 非非负整数、`revocable` 非布尔等） |
| `E_ID` / `E_DUP_ID` | 标识缺失/非字符串，或 ID 重复 |
| `E_DIGEST` | 摘要缺失或非字符串 |
| `E_UNKNOWN_REF` | 引用了不存在的区块/事务/采集器/分片 |
| `E_EMPTY_WRITES` | 事务没有任何写入项 |
| `E_LOCAL_SEQ_CONFLICT` | 同采集器本地序号重复 |
| `E_CAUSAL_CYCLE` | 因果偏序存在环（含自环） |
| `E_WRITE_INCOMPARABLE` | 同块两个写事务无法由因果排序 |
| `E_BARRIER_CONFLICT` | 同一事务被同一分片同时要求包含与排除 |
| `E_COST_MISSING` | 拥有可撤分片的采集器未声明隔离代价 |
| `E_FIXED_REVOCABLE` | 固定证据引用了可撤分片 |

语义不可行（无共同切片）不是结构错误：退出码仍为 0，在 JSON 中以
`feasible:false` 表达。
