"""输入解析与结构校验、偏序（DAG）构建。

所有“结构错误”在本模块抛出 :class:`~solution.errors.InputError`，
带有错误码与字段位置；语义层面的“无可行切片”不是结构错误。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .errors import InputError


@dataclass(frozen=True)
class Txn:
    tid: str
    collector: str
    seq: int
    prereqs: tuple[str, ...]
    writes: dict[str, str]
    actor: str


@dataclass(frozen=True)
class Shard:
    sid: str
    collector: str
    revocable: bool
    must_include: tuple[str, ...]
    must_exclude: tuple[str, ...]
    observations: tuple[tuple[str, str], ...]


@dataclass
class Case:
    raw: dict[str, Any]
    case_id: str
    media_id: str
    block_ids: list[str]
    initial_digest: dict[str, str]
    initial_source: dict[str, str]
    collectors: dict[str, dict[str, Any]]
    txns: list[Txn]
    shards: list[Shard]
    order: list[str]                       # 拓扑序事务 ID（确定性）
    pos: dict[str, int]                    # 事务 -> 拓扑位置
    index: dict[str, Txn]                  # 事务 ID -> Txn
    down: list[int]                        # 每事务（按拓扑位）的下闭包位掩码
    reach: list[list[bool]]                # reach[u][v]：u 是否先于 v
    block_writers: dict[str, list[str]]    # 区块 -> 写过它的事务（拓扑序）
    compare_cfg: dict[str, Any] | None
    suspect_cfg: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# 基础校验工具
# --------------------------------------------------------------------------- #

def _is_int(x: Any) -> bool:
    # bool 是 int 的子类，显式排除
    return isinstance(x, int) and not isinstance(x, bool)


def _require(cond: bool, code: str, path: str, message: str) -> None:
    if not cond:
        raise InputError(code, path, message)


def _check_id(value: Any, path: str) -> str:
    _require(isinstance(value, str) and value != "", "E_ID", path, "标识必须是非空字符串")
    return value


def _check_str(value: Any, path: str) -> str:
    _require(isinstance(value, str), "E_TYPE", path, "必须是字符串")
    return value


def _check_digest(value: Any, path: str) -> str:
    _require(isinstance(value, str) and value != "", "E_DIGEST", path, "摘要必须是非空字符串")
    return value


def _as_list(value: Any, path: str, code: str = "E_TYPE") -> list:
    _require(isinstance(value, list), code, path, "必须是数组")
    return value


# --------------------------------------------------------------------------- #
# 分区解析
# --------------------------------------------------------------------------- #

def _parse_blocks(raw: Any) -> tuple[list[str], dict[str, str], dict[str, str]]:
    path = "blocks"
    blocks = _as_list(raw, path)
    block_ids: list[str] = []
    initial_digest: dict[str, str] = {}
    initial_source: dict[str, str] = {}
    for i, b in enumerate(blocks):
        bp = f"{path}[{i}]"
        _require(isinstance(b, dict), "E_TYPE", bp, "区块必须是对象")
        bid = _check_id(b.get("id"), f"{bp}.id")
        _require(bid not in initial_digest, "E_DUP_ID", f"{bp}.id", f"区块 ID 重复: {bid}")
        initial_digest[bid] = _check_digest(b.get("initial_digest"), f"{bp}.initial_digest")
        # 初始来源可显式声明，缺省用确定性的虚构来源名
        src = b.get("initial_source", f"INIT:{bid}")
        initial_source[bid] = _check_str(src, f"{bp}.initial_source")
        block_ids.append(bid)
    return block_ids, initial_digest, initial_source


# --------------------------------------------------------------------------- #
# 采集器解析
# --------------------------------------------------------------------------- #

def _parse_collectors(raw: Any) -> dict[str, dict[str, Any]]:
    path = "collectors"
    items = _as_list(raw, path)
    collectors: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(items):
        cp = f"{path}[{i}]"
        _require(isinstance(c, dict), "E_TYPE", cp, "采集器必须是对象")
        cid = _check_id(c.get("id"), f"{cp}.id")
        _require(cid not in collectors, "E_DUP_ID", f"{cp}.id", f"采集器 ID 重复: {cid}")
        marker = c.get("trust_marker", f"TM:{cid}")
        collectors[cid] = {"trust_marker": _check_str(marker, f"{cp}.trust_marker")}
    return collectors


# --------------------------------------------------------------------------- #
# 事务解析
# --------------------------------------------------------------------------- #

def _parse_txns(raw: Any, collectors: dict[str, dict[str, Any]]) -> list[Txn]:
    path = "transactions"
    items = _as_list(raw, path)
    txns: list[Txn] = []
    seen: set[str] = set()
    for i, t in enumerate(items):
        tp = f"{path}[{i}]"
        _require(isinstance(t, dict), "E_TYPE", tp, "事务必须是对象")
        tid = _check_id(t.get("id"), f"{tp}.id")
        _require(tid not in seen, "E_DUP_ID", f"{tp}.id", f"事务 ID 重复: {tid}")
        seen.add(tid)

        cid = _check_id(t.get("collector"), f"{tp}.collector")
        _require(cid in collectors, "E_UNKNOWN_REF", f"{tp}.collector",
                 f"事务 {tid} 引用未知采集器 {cid}")

        seq = t.get("local_seq")
        _require(_is_int(seq) and seq >= 0, "E_TYPE", f"{tp}.local_seq",
                 "local_seq 必须是非负整数")

        prereq_raw = t.get("causal_after", [])
        prereq_list = _as_list(prereq_raw, f"{tp}.causal_after")
        prereqs: list[str] = []
        for j, p in enumerate(prereq_list):
            prereqs.append(_check_id(p, f"{tp}.causal_after[{j}]"))

        writes_raw = t.get("writes")
        writes_list = _as_list(writes_raw, f"{tp}.writes")
        _require(len(writes_list) > 0, "E_EMPTY_WRITES", f"{tp}.writes",
                 f"事务 {tid} 至少修改一个区块（原子多区块事务）")
        writes: dict[str, str] = {}
        for j, w in enumerate(writes_list):
            wp = f"{tp}.writes[{j}]"
            _require(isinstance(w, dict), "E_TYPE", wp, "写入项必须是对象")
            bid = _check_id(w.get("block"), f"{wp}.block")
            digest = _check_digest(w.get("digest"), f"{wp}.digest")
            _require(bid not in writes, "E_DUP_ID", f"{wp}.block",
                     f"事务 {tid} 内区块 {bid} 出现多次")
            writes[bid] = digest

        actor = _check_str(t.get("actor", f"ACTOR:{tid}"), f"{tp}.actor")
        txns.append(Txn(tid=tid, collector=cid, seq=int(seq),
                        prereqs=tuple(prereqs), writes=writes, actor=actor))
    return txns


# --------------------------------------------------------------------------- #
# 分片解析
# --------------------------------------------------------------------------- #

def _parse_shards(raw: Any, case_blocks: set[str], txn_ids: set[str],
                  collectors: dict[str, dict[str, Any]]) -> list[Shard]:
    path = "shards"
    items = _as_list(raw, path, code="E_MISSING")
    shards: list[Shard] = []
    seen: set[str] = set()
    for i, s in enumerate(items):
        sp = f"{path}[{i}]"
        _require(isinstance(s, dict), "E_TYPE", sp, "分片必须是对象")
        sid = _check_id(s.get("id"), f"{sp}.id")
        _require(sid not in seen, "E_DUP_ID", f"{sp}.id", f"分片 ID 重复: {sid}")
        seen.add(sid)

        cid = _check_id(s.get("collector"), f"{sp}.collector")
        _require(cid in collectors, "E_UNKNOWN_REF", f"{sp}.collector",
                 f"分片 {sid} 引用未知采集器 {cid}")

        rev = s.get("revocable", True)
        _require(isinstance(rev, bool), "E_TYPE", f"{sp}.revocable", "必须是布尔值")

        barriers = s.get("barriers")
        _require(isinstance(barriers, dict), "E_TYPE", f"{sp}.barriers",
                 "barriers 必须是对象（含 must_include / must_exclude）")
        mi_raw = _as_list(barriers.get("must_include", []), f"{sp}.barriers.must_include")
        me_raw = _as_list(barriers.get("must_exclude", []), f"{sp}.barriers.must_exclude")
        must_include: list[str] = []
        for j, x in enumerate(mi_raw):
            t = _check_id(x, f"{sp}.barriers.must_include[{j}]")
            _require(t in txn_ids, "E_UNKNOWN_REF", f"{sp}.barriers.must_include[{j}]",
                     f"分片 {sid} 屏障引用未知事务 {t}")
            must_include.append(t)
        must_exclude: list[str] = []
        for j, x in enumerate(me_raw):
            t = _check_id(x, f"{sp}.barriers.must_exclude[{j}]")
            _require(t in txn_ids, "E_UNKNOWN_REF", f"{sp}.barriers.must_exclude[{j}]",
                     f"分片 {sid} 屏障引用未知事务 {t}")
            must_exclude.append(t)
        overlap = set(must_include) & set(must_exclude)
        _require(not overlap, "E_BARRIER_CONFLICT", f"{sp}.barriers",
                 f"分片 {sid} 同一事务同时被要求包含与排除: {sorted(overlap)}")

        obs_raw = _as_list(s.get("observations", []), f"{sp}.observations")
        observations: list[tuple[str, str]] = []
        obs_blocks: set[str] = set()
        for j, o in enumerate(obs_raw):
            op = f"{sp}.observations[{j}]"
            _require(isinstance(o, dict), "E_TYPE", op, "观察项必须是对象")
            bid = _check_id(o.get("block"), f"{op}.block")
            _require(bid in case_blocks, "E_UNKNOWN_REF", f"{op}.block",
                     f"分片 {sid} 观察未知区块 {bid}")
            _require(bid not in obs_blocks, "E_DUP_ID", f"{op}.block",
                     f"分片 {sid} 对区块 {bid} 声明了多个观察摘要")
            obs_blocks.add(bid)
            digest = _check_digest(o.get("digest"), f"{op}.digest")
            observations.append((bid, digest))

        shards.append(Shard(sid=sid, collector=cid, revocable=bool(rev),
                            must_include=tuple(must_include),
                            must_exclude=tuple(must_exclude),
                            observations=tuple(observations)))
    return shards


# --------------------------------------------------------------------------- #
# compare / suspect 段解析
# --------------------------------------------------------------------------- #

def _parse_compare(raw: Any, block_ids: set[str], txn_ids: set[str]) -> dict[str, Any] | None:
    cfg = raw.get("compare")
    if cfg is None:
        return None
    path = "compare"
    _require(isinstance(cfg, dict), "E_TYPE", path, "compare 必须是对象")

    wb_raw = _as_list(cfg.get("witness_blocks"), f"{path}.witness_blocks")
    witness: list[str] = []
    for j, b in enumerate(wb_raw):
        bid = _check_id(b, f"{path}.witness_blocks[{j}]")
        _require(bid in block_ids, "E_UNKNOWN_REF", f"{path}.witness_blocks[{j}]",
                 f"见证区块未知: {bid}")
        witness.append(bid)

    slices_raw = _as_list(cfg.get("slices"), f"{path}.slices")
    _require(len(slices_raw) == 2, "E_TYPE", f"{path}.slices", "必须给出两个候选切片")
    slices: list[list[str]] = []
    for j, sl in enumerate(slices_raw):
        sp = f"{path}.slices[{j}]"
        arr = _as_list(sl, sp)
        cur: list[str] = []
        seen: set[str] = set()
        for k, t in enumerate(arr):
            tid = _check_id(t, f"{sp}[{k}]")
            _require(tid in txn_ids, "E_UNKNOWN_REF", f"{sp}[{k}]", f"未知事务 {tid}")
            _require(tid not in seen, "E_DUP_ID", f"{sp}[{k}]", f"切片内事务重复: {tid}")
            seen.add(tid)
            cur.append(tid)
        slices.append(cur)

    pm_raw = cfg.get("preservation")
    _require(isinstance(pm_raw, dict), "E_TYPE", f"{path}.preservation",
             "preservation 必须是 区块->摘要 的对象")
    preservation: dict[str, str] = {}
    for bid, digest in pm_raw.items():
        _require(bid in block_ids, "E_UNKNOWN_REF", f"{path}.preservation.{bid}",
                 f"保全镜像引用未知区块 {bid}")
        preservation[bid] = _check_digest(digest, f"{path}.preservation.{bid}")

    return {"witness_blocks": witness, "slices": slices, "preservation": preservation}


def _parse_suspect(raw: Any, collectors: dict[str, dict[str, Any]],
                   shards: list[Shard]) -> dict[str, Any] | None:
    cfg = raw.get("suspect")
    if cfg is None:
        return None
    path = "suspect"
    _require(isinstance(cfg, dict), "E_TYPE", path, "suspect 必须是对象")

    costs_raw = cfg.get("isolation_costs")
    _require(isinstance(costs_raw, dict), "E_TYPE", f"{path}.isolation_costs",
             "isolation_costs 必须是 采集器->代价 的对象")
    costs: dict[str, int] = {}
    for cid, cost in costs_raw.items():
        _require(cid in collectors, "E_UNKNOWN_REF", f"{path}.isolation_costs.{cid}",
                 f"未知采集器 {cid}")
        _require(_is_int(cost) and cost >= 0, "E_TYPE",
                 f"{path}.isolation_costs.{cid}", "隔离代价必须是非负整数")
        costs[cid] = int(cost)

    # 拥有可撤分片的采集器必须给出代价，否则无法被隔离
    for s in shards:
        if s.revocable and s.collector not in costs:
            raise InputError(
                "E_COST_MISSING", f"{path}.isolation_costs",
                f"采集器 {s.collector} 拥有可撤分片 {s.sid}，必须声明隔离代价")

    fixed_raw = cfg.get("fixed_evidence", [])
    fixed = _as_list(fixed_raw, f"{path}.fixed_evidence")
    shard_ids = {s.sid for s in shards}
    fixed_evidence: list[str] = []
    for j, sid in enumerate(fixed):
        _check_id(sid, f"{path}.fixed_evidence[{j}]")
        _require(sid in shard_ids, "E_UNKNOWN_REF", f"{path}.fixed_evidence[{j}]",
                 f"固定证据引用未知分片 {sid}")
        fixed_evidence.append(sid)
    # 固定证据名单里的分片本身必须是不可撤的
    for sid in fixed_evidence:
        s = next(x for x in shards if x.sid == sid)
        _require(not s.revocable, "E_FIXED_REVOCABLE", f"{path}.fixed_evidence",
                 f"分片 {sid} 标记为可撤，不能作为固定证据")

    return {"costs": costs, "fixed_evidence": fixed_evidence}


# --------------------------------------------------------------------------- #
# 偏序构建
# --------------------------------------------------------------------------- #

def _build_order(case_blocks: set[str], txns: list[Txn]) -> tuple[
        list[str], dict[str, int], list[int], list[list[bool]], dict[str, list[str]]]:
    ids = [t.tid for t in txns]
    txn_ids = set(ids)
    n = len(txns)
    index = {t.tid: t for t in txns}
    edges: set[tuple[str, str]] = set()

    # 跨采集器因果（输入声明）
    for t in txns:
        for p in t.prereqs:
            if p not in txn_ids:
                raise InputError("E_UNKNOWN_REF", "transactions",
                                 f"事务 {t.tid} 的因果先决 {p} 不存在")
            if p == t.tid:
                raise InputError("E_CAUSAL_CYCLE", "transactions",
                                 f"事务 {t.tid} 不能以自身为先决")
            edges.add((p, t.tid))

    # 同一采集器：本地日志序号排序产生全序
    by_collector: dict[str, list[Txn]] = {}
    for t in txns:
        by_collector.setdefault(t.collector, []).append(t)
    for cid, group in by_collector.items():
        ordered = sorted(group, key=lambda x: x.seq)
        seqs = [x.seq for x in ordered]
        if len(set(seqs)) != len(seqs):
            dup = sorted({s for s in seqs if seqs.count(s) > 1})
            raise InputError("E_LOCAL_SEQ_CONFLICT", "transactions",
                             f"采集器 {cid} 存在重复 local_seq: {dup}")
        for a, b in zip(ordered, ordered[1:]):
            edges.add((a.tid, b.tid))

    # Kahn 拓扑（就绪队列按事务 ID 排序，保证确定性）
    adj: dict[str, set[str]] = {tid: set() for tid in ids}
    indeg: dict[str, int] = {tid: 0 for tid in ids}
    for a, b in edges:
        if b not in adj[a]:
            adj[a].add(b)
            indeg[b] += 1
    ready = sorted(tid for tid, d in indeg.items() if d == 0)
    order: list[str] = []
    deque_ready = deque(ready)
    in_ready = set(ready)
    while deque_ready:
        cur = deque_ready.popleft()
        in_ready.discard(cur)
        order.append(cur)
        for nxt in sorted(adj[cur]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0 and nxt not in in_ready:
                # 插入并保持有序（规模小，直接重排）
                deque_ready.append(nxt)
                in_ready.add(nxt)
                # 重新排序以维持确定性
                items = sorted(deque_ready)
                deque_ready.clear()
                deque_ready.extend(items)
    if len(order) != n:
        remaining = sorted(tid for tid in ids if tid not in set(order))
        raise InputError("E_CAUSAL_CYCLE", "transactions",
                         f"事务偏序存在环，环上事务至少包含: {remaining}")

    pos = {tid: i for i, tid in enumerate(order)}

    # 前驱邻接表：pred[b] = 所有满足 a -> b 的 a
    pred: dict[str, set[str]] = {tid: set() for tid in ids}
    for a, b in edges:
        pred[b].add(a)

    # 下闭包（先决闭包）位掩码：沿拓扑序正向，
    # down[u] = {u} ∪ 全部直接/间接先决。
    down = [0] * n
    for tid in order:
        u = pos[tid]
        mask = 1 << u
        for p in pred[tid]:
            mask |= down[pos[p]]
        down[u] = mask

    # reach[u][v]：u 是否先于 v（u 出现在 v 的下闭包中）
    reach = [[False] * n for _ in range(n)]
    for v in range(n):
        m = down[v]
        for u in range(n):
            if u != v and ((m >> u) & 1):
                reach[u][v] = True

    # 每个区块的写者链，并检查“同块写必可排序”的结构约束
    block_writers: dict[str, list[str]] = {b: [] for b in case_blocks}
    for tid in order:
        for b in index[tid].writes:
            block_writers[b].append(tid)
    for b, writers in block_writers.items():
        for i in range(len(writers)):
            for j in range(i + 1, len(writers)):
                u, v = pos[writers[i]], pos[writers[j]]
                if not reach[u][v]:  # 拓扑序 i<j，只需确认 u 先于 v
                    raise InputError(
                        "E_WRITE_INCOMPARABLE", "transactions",
                        f"区块 {b} 的两个写事务 {writers[i]} 与 {writers[j]} "
                        f"无法由因果关系排序")
    return order, pos, down, reach, block_writers


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def parse_case(raw: Any) -> Case:
    _require(isinstance(raw, dict), "E_TYPE", "$", "顶层必须是 JSON 对象")
    case_id = _check_str(raw.get("case_id", "case"), "case_id")
    media_id = _check_str(raw.get("media_id", "fictional-media"), "media_id")

    block_ids, initial_digest, initial_source = _parse_blocks(raw.get("blocks"))
    block_set = set(block_ids)
    collectors = _parse_collectors(raw.get("collectors"))
    txns = _parse_txns(raw.get("transactions"), collectors)

    # 写入区块必须存在
    txn_ids = {t.tid for t in txns}
    for t in txns:
        for b in t.writes:
            if b not in block_set:
                raise InputError("E_UNKNOWN_REF", "transactions",
                                 f"事务 {t.tid} 写入未知区块 {b}")

    shards = _parse_shards(raw.get("shards", []), block_set, txn_ids, collectors)
    order, pos, down, reach, block_writers = _build_order(block_set, txns)
    compare_cfg = _parse_compare(raw, block_set, txn_ids)
    suspect_cfg = _parse_suspect(raw, collectors, shards)

    return Case(raw=raw, case_id=case_id, media_id=media_id,
                block_ids=block_ids, initial_digest=initial_digest,
                initial_source=initial_source, collectors=collectors,
                txns=txns, shards=shards, order=order, pos=pos,
                index={t.tid: t for t in txns}, down=down, reach=reach,
                block_writers=block_writers, compare_cfg=compare_cfg,
                suspect_cfg=suspect_cfg)
