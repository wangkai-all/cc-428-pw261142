"""加工执行器 + 差异分析核心。

三条路径：
  NAIVE 朴素明细连接：把所有表连接后直接 SUM。永远执行，用作诊断基线，
        用来"展示金额/计数为何被放大"，不允许拿它的结果静默去重充当正确值。
  ATJ   先聚合再关联：仅当路径上每条边都是"多对一且一端键运行期唯一"、
        且度量可加时才合法；否则拒绝并给出具体原因。
  DJA   明细连接后按贡献归并：每条扇出边必须登记分摊基准（多端权重列），
        且每个父键下权重之和为 1；连接明细按权重乘积折算后再聚合。

关键原则：不满足条件的查询被显式拒绝（REJECTED + 原因），
绝不靠事后 DISTINCT/去重来掩盖错误关联。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import store

PROV = "__prov__"
FACTOR = "__weight_factor__"
TOL = 1e-9

PATH_NAIVE = "NAIVE"
PATH_ATJ = "ATJ"
PATH_DJA = "DJA"


class EngineError(Exception):
    pass


# ---------------------------------------------------------------------------
# 连接路径规划：在"已启用关系"构成的无向图上 BFS
# ---------------------------------------------------------------------------
@dataclass
class Edge:
    rel: dict
    from_table: str
    to_table: str
    left_cols: list          # 遍历方向上的连接列（from 侧）
    right_cols: list         # to 侧
    fanout: bool             # 该方向是否复制 from 侧行
    factor_col: str | None = None  # DJA 折算权重列（位于多端行上）
    reverse: bool = False
    bridge: bool = False     # 桥接展开产生的合成边
    bridge_half: str = ""    # "left"（左表->桥）或 "right"（桥->右表）


def _build_graph(relationships: list[dict]) -> dict[str, list[dict]]:
    """无向图。M:N 边登记了桥接表；桥接表自身的两段 N:1 边标记为
    bridge_only，不参与 BFS 寻路，只在 M:N 边展开时显式使用。"""
    graph: dict[str, list[dict]] = defaultdict(list)
    bridge_tables = {rel["join_through"]["bridge"]
                     for rel in relationships if rel.get("join_through")}
    for rel in relationships:
        if rel["left_table"] in bridge_tables or rel["right_table"] in bridge_tables:
            rel = {**rel, "bridge_only": True}
        graph[rel["left_table"]].append(rel)
        graph[rel["right_table"]].append(rel)
    return graph


def _edge_direction(rel: dict, from_table: str, to_table: str) -> Edge:
    reverse = from_table == rel["right_table"] and to_table == rel["left_table"]
    if reverse:
        left_cols, right_cols = rel["right_cols"], rel["left_cols"]
    else:
        left_cols, right_cols = rel["left_cols"], rel["right_cols"]
    card = rel["cardinality"]
    if card == "M:N":
        fanout = True
    elif card == "N:1":
        fanout = reverse          # 正向 child->parent 不扇出；反向 parent->child 扇出
    elif card == "1:N":
        fanout = not reverse
    else:
        raise EngineError(f"未知基数 {card}")
    factor_col = None
    if fanout and rel.get("basis_col"):
        # 基准列登记在多端表；遍历中多端总是被连接进来的那一侧
        factor_col = rel["basis_col"]
    return Edge(rel=rel, from_table=from_table, to_table=to_table,
                left_cols=left_cols, right_cols=right_cols,
                fanout=fanout, factor_col=factor_col, reverse=reverse)


def _expand_mn_edge(rel: dict) -> list[Edge]:
    """M:N 边展开为：左表 -> 桥接表 -> 右表，两跳都标记 fanout。

    列配对（关键）：
      第一跳 左表连接键(rel.left_cols) = 桥表上的左连接键(jt.left_bridge_cols)
      第二跳 桥表上的右连接键(jt.right_bridge_cols) = 右表连接键(rel.right_cols)
    """
    jt = rel["join_through"]
    bridge = jt["bridge"]
    e1 = Edge(rel=rel, from_table=rel["left_table"], to_table=bridge,
              left_cols=rel["left_cols"], right_cols=jt["left_bridge_cols"],
              fanout=True, reverse=False, bridge=True, bridge_half="left")
    e2 = Edge(rel=rel, from_table=bridge, to_table=rel["right_table"],
              left_cols=jt["right_bridge_cols"], right_cols=rel["right_cols"],
              fanout=True, reverse=False, bridge=True, bridge_half="right")
    return [e1, e2]


def _materialize_edges(rel: dict, from_table: str, to_table: str) -> list[Edge]:
    """把一条规划选中的关系物化为一个或多个执行边（M:N 展开桥接）。"""
    if rel["cardinality"] == "M:N" and rel.get("join_through"):
        edges = _expand_mn_edge(rel)
        if from_table == rel["right_table"]:
            edges = [Edge(rel=e.rel, from_table=e.to_table, to_table=e.from_table,
                          left_cols=e.right_cols, right_cols=e.left_cols,
                          fanout=True, reverse=not e.reverse, bridge=True,
                          bridge_half=e.bridge_half) for e in reversed(edges)]
        return edges
    return [_edge_direction(rel, from_table, to_table)]


def plan_path(conn, spec: dict) -> tuple[list[Edge], list[dict]]:
    """返回 (有序边列表, 规划错误列表)。"""
    errors: list[dict] = []
    fact = spec["fact_table"]
    rels = store.list_relationships(conn, active_only=True)
    graph = _build_graph(rels)

    # 找 group_by 列所在的表
    target_tables: set[str] = set()
    for col in spec["group_by"]:
        owners = [m["table_name"] for m in store.list_table_meta(conn)
                  for c in m["columns"] if c["col_name"] == col]
        if not owners:
            errors.append({"code": "UNKNOWN_COLUMN", "detail": f"维度列 {col} 未在粒度登记中找到"})
            continue
        if len(owners) > 1:
            errors.append({"code": "AMBIGUOUS_COLUMN",
                           "detail": f"列 {col} 同时存在于 {owners}，无法确定关联路径"})
            continue
        target_tables.add(owners[0])

    # BFS：fact -> 每个目标表
    edges: list[Edge] = []
    reached = {fact}
    for target in target_tables:
        if target == fact:
            continue
        prev = bfs(graph, fact, target)
        if prev is None:
            errors.append({"code": "NO_PATH",
                           "detail": f"{fact} 到 {target} 之间不存在已启用的关系路径"})
            continue
        # 回溯路径（M:N 关系在此展开为桥接两跳）
        chain = []
        cur = target
        while cur != fact:
            rel_name, nxt = prev[cur]
            rel = next(r for r in rels if r["name"] == rel_name)
            # 回溯是 target->fact，插入头部以恢复 fact->target 的执行顺序
            chain = _materialize_edges(rel, nxt, cur) + chain
            cur = nxt
        for edge in chain:
            if edge.to_table not in reached:
                edges.append(edge)
                reached.add(edge.to_table)
    return edges, errors


def bfs(graph, start, target):
    queue = [start]
    seen = {start}
    prev = {}
    while queue:
        node = queue.pop(0)
        if node == target:
            return prev
        for rel in graph.get(node, []):
            if rel.get("bridge_only"):
                continue
            nbr = rel["right_table"] if rel["left_table"] == node else rel["left_table"]
            if nbr in seen:
                continue
            seen.add(nbr)
            prev[nbr] = (rel["name"], node)
            queue.append(nbr)
    return None


# ---------------------------------------------------------------------------
# 数据装载与哈希连接（带 provenance）
# ---------------------------------------------------------------------------
def _load_table(conn, table: str) -> list[dict]:
    meta = next((m for m in store.list_table_meta(conn) if m["table_name"] == table), None)
    if meta is None:
        raise EngineError(f"表 {table} 未做粒度登记")
    pk = meta["pk_cols"]
    raw_rows = store.get_rows(conn, table)
    rows = []
    for src in raw_rows:
        # 深拷贝：同一物理表会被多次加载，行对象在连接中被改写因子/来源，
        # 必须彼此隔离，不能共享可变状态
        row = {k: (list(v) if isinstance(v, list) else v) for k, v in src.items()}
        # provenance 用不可变元组 (表名, (键值元组))，避免可变 dict 别名污染
        row[PROV] = [(table, tuple((k, row[k]) for k in pk))]
        row.setdefault(FACTOR, 1.0)
        rows.append(row)
    return rows


def _key(row, cols):
    return tuple(row[c] for c in cols)


def hash_join(left, right, lcols, rcols):
    """内连接，返回 (连接行, 左侧未匹配键集合, 右侧未匹配键集合)。"""
    index: dict[tuple, list[dict]] = defaultdict(list)
    for r in right:
        index[_key(r, rcols)].append(r)
    out = []
    unmatched_left = set()
    matched_right_keys = set()
    for lrow in left:
        k = _key(lrow, lcols)
        matches = index.get(k)
        if not matches:
            unmatched_left.add(k)
            continue
        matched_right_keys.add(k)
        for rrow in matches:
            # 深拷贝左行：连接是多阶段流水线，浅拷贝会让上一跳的行对象
            # 在后续跳中被原地改写（因子/列），造成跨结果的别名污染
            merged = {k: (list(v) if isinstance(v, list) else v)
                      for k, v in lrow.items()}
            # 右表列写入；连接键保留原名（同一语义键在两端本就相等），
            # 其余重名列加表前缀，避免覆盖
            join_cols = set(rcols)
            for col, val in rrow.items():
                if col in (PROV, FACTOR):
                    continue
                if col in join_cols:
                    merged[col] = val
                elif col not in merged:
                    merged[col] = val
                else:
                    merged[f"{rrow[PROV][-1][0]}.{col}"] = val
            merged[PROV] = list(lrow[PROV]) + list(rrow[PROV])
            merged[FACTOR] = lrow[FACTOR] * rrow[FACTOR]
            out.append(merged)
    unmatched_left = {_key(r, lcols) for r in left if _key(r, lcols) not in matched_right_keys}
    right_keys = {_key(r, rcols) for r in right}
    unmatched_right = right_keys - matched_right_keys
    return out, unmatched_left, unmatched_right


def _duplicate_keys(rows, cols) -> dict[tuple, int]:
    counts = defaultdict(int)
    for r in rows:
        counts[_key(r, cols)] += 1
    return {k: c for k, c in counts.items() if c > 1}


# ---------------------------------------------------------------------------
# 度量元数据校验
# ---------------------------------------------------------------------------
def _measure_meta(conn, spec) -> list[dict]:
    out = []
    fact_cols = {c["col_name"]: c for c in store.get_columns(conn, spec["fact_table"])}
    for m in spec["measures"]:
        col, agg = m["col"], m.get("agg", "sum")
        if col not in fact_cols:
            raise EngineError(f"度量列 {col} 不在事实表 {spec['fact_table']} 上")
        if agg != "sum":
            raise EngineError(f"暂仅支持 sum 聚合，收到 {agg}")
        out.append({"col": col, "additive": bool(fact_cols[col]["additive"])})
    return out


# ---------------------------------------------------------------------------
# 运行期唯一性 / 权重完整性检查
# ---------------------------------------------------------------------------
def _check_uniqueness(conn, edge: Edge) -> list[dict]:
    """检查 N:1 边的一端连接键在运行数据中是否唯一。"""
    problems = []
    if edge.fanout:
        return problems
    right_rows = _load_table(conn, edge.to_table)
    dup = _duplicate_keys(right_rows, edge.right_cols)
    if dup:
        sample = [{"key": list(k), "rows": c} for k, c in list(dup.items())[:5]]
        problems.append({
            "code": "NON_UNIQUE_PARENT_KEY",
            "edge": edge.rel["name"],
            "detail": f"表 {edge.to_table} 的连接键 {edge.right_cols} 存在重复值，"
                      f"名义 N:1 关系在运行期退化为一对多，连接将产生扇出",
            "sample": sample,
        })
    return problems


def _check_basis_completeness(conn, edge: Edge) -> list[dict]:
    """检查扇出边的分摊基准：权重列存在，且每个父键下权重之和 = 1。"""
    problems = []
    rel = edge.rel
    if not edge.fanout:
        return problems
    if edge.bridge:
        problems.append({
            "code": "MISSING_ALLOCATION_BASIS",
            "edge": rel["name"],
            "detail": f"M:N 关系 {rel['name']} 经桥接表 {rel['join_through']['bridge']} "
                      f"连接（{edge.from_table}->{edge.to_table}）会复制事实行，"
                      f"且未登记任何分摊基准，无法按贡献归并",
        })
        return problems
    if not rel.get("basis_table") or not rel.get("basis_col"):
        problems.append({
            "code": "MISSING_ALLOCATION_BASIS",
            "edge": rel["name"],
            "detail": f"边 {rel['name']}（{edge.from_table}->{edge.to_table}）会复制事实行，"
                      f"但未登记分摊基准，无法按贡献归并",
        })
        return problems
    child_table = rel["basis_table"]
    basis_col = rel["basis_col"]
    cols = store.get_columns(conn, child_table)
    if not any(c["col_name"] == basis_col for c in cols):
        problems.append({
            "code": "BAD_BASIS_COLUMN",
            "edge": rel["name"],
            "detail": f"基准列 {child_table}.{basis_col} 不存在",
        })
        return problems
    child_rows = _load_table(conn, child_table)
    # 多端（子）表上对应的连接列与父端连接列
    if rel["left_table"] == child_table:
        child_join_cols, parent_join_cols = rel["left_cols"], rel["right_cols"]
    else:
        child_join_cols, parent_join_cols = rel["right_cols"], rel["left_cols"]
    del parent_join_cols  # 仅用于说明父子角色，权重按子端连接键分组即可
    sums = defaultdict(float)
    for r in child_rows:
        sums[_key(r, child_join_cols)] += float(r[basis_col])
    bad = [{"parent_key": list(k), "weight_sum": round(v, 6)}
           for k, v in sums.items() if abs(v - 1.0) > TOL]
    if bad:
        problems.append({
            "code": "INCOMPLETE_WEIGHTS",
            "edge": rel["name"],
            "detail": f"分摊基准 {child_table}.{basis_col} 在部分父键下权重之和不等于 1，"
                      f"归并后总金额会被改变（这本身就是数据错误，不能静默归一化）",
            "sample": bad[:5],
        })
    return problems


# ---------------------------------------------------------------------------
# 公共：沿路径连接，记录每阶段行数
# ---------------------------------------------------------------------------
def _connect_path(conn, spec, edges, apply_factor=False):
    fact_rows = _load_table(conn, spec["fact_table"])
    stage_counts = [{"stage": f"事实表 {spec['fact_table']}", "rows": len(fact_rows)}]
    current = fact_rows
    orphan_notes = []
    for edge in edges:
        right_rows = _load_table(conn, edge.to_table)
        if apply_factor and edge.fanout and edge.factor_col:
            # 权重在多端（右）行上；连接后因子取该行权重
            for r in right_rows:
                r[FACTOR] = float(r[edge.factor_col])
        joined, unl, unr = hash_join(current, right_rows, edge.left_cols, edge.right_cols)
        stage_counts.append({
            "stage": f"{edge.from_table} JOIN {edge.to_table} ({edge.rel['name']}"
                     f"{' [扇出]' if edge.fanout else ''})",
            "rows": len(joined),
        })
        if unl:
            orphan_notes.append({
                "code": "ORPHAN_CHILD_ROWS",
                "edge": edge.rel["name"],
                "detail": f"{edge.from_table} 有 {len(unl)} 个连接键在 {edge.to_table} 中无匹配，"
                          f"内连接会丢失这些行（度量被静默吞掉）",
                "sample": [list(k) for k in list(unl)[:5]],
            })
        current = joined
    return current, stage_counts, orphan_notes


def _group_sum(rows, group_cols, measure_cols, weighted=False):
    agg = defaultdict(lambda: {c: 0.0 for c in measure_cols})
    for r in rows:
        key = tuple(r.get(g) for g in group_cols)
        factor = r.get(FACTOR, 1.0) if weighted else 1.0
        for c in measure_cols:
            agg[key][c] += float(r[c]) * factor
    return [{"group": list(k), **{c: round(v, 6) for c, v in vals.items()}}
            for k, vals in sorted(agg.items(), key=lambda kv: tuple(map(str, kv[0])))]


# ---------------------------------------------------------------------------
# NAIVE：诊断基线，永远执行
# ---------------------------------------------------------------------------
def run_naive(conn, spec, edges, measure_meta):
    joined, stage_counts, orphans = _connect_path(conn, spec, edges, apply_factor=False)
    measure_cols = [m["col"] for m in measure_meta]
    result = _group_sum(joined, spec["group_by"], measure_cols, weighted=False)

    # 贡献账本：每个事实行在连接结果中出现几次；各度量分别记账，绝不混加
    fact_pk = next(m["pk_cols"] for m in store.list_table_meta(conn)
                   if m["table_name"] == spec["fact_table"])
    # ledger[fid] = {copies, by_measure: {col: {"total":x, "groups":{gk:v}}}}
    def _empty_entry():
        return {"copies": 0,
                "by_measure": {c: {"total": 0.0, "groups": defaultdict(float)}
                               for c in measure_cols}}
    ledger = defaultdict(_empty_entry)
    for r in joined:
        fact_ref = dict(next(p for t, p in r[PROV] if t == spec["fact_table"]))
        fid = tuple(fact_ref[k] for k in fact_pk)
        entry = ledger[fid]
        entry["copies"] += 1
        gk = tuple(r.get(g) for g in spec["group_by"])
        for c in measure_cols:
            val = float(r[c])
            entry["by_measure"][c]["total"] += val
            entry["by_measure"][c]["groups"][gk] += val

    duplicates = []
    for fid, entry in ledger.items():
        if entry["copies"] > 1:
            per_measure = {}
            for c, bucket in entry["by_measure"].items():
                # 该事实行的原始（未复制）度量值 = 总量 / 副本数
                per_measure[c] = {
                    "repeated_total": round(bucket["total"], 6),
                    "single_value": round(bucket["total"] / entry["copies"], 6),
                    "by_group": [{"group": list(g), "amount": round(v, 6)}
                                 for g, v in bucket["groups"].items()],
                }
            duplicates.append({
                "fact_pk": fact_pk,
                "fact_key": list(fid),
                "copies": entry["copies"],
                "measures": per_measure,
            })
    # 按第一个度量的重复总量排序，便于优先展示金额放大最严重的行
    first = measure_cols[0]
    duplicates.sort(key=lambda d: -d["measures"][first]["repeated_total"])

    fact_rows = _load_table(conn, spec["fact_table"])
    base_totals = {c: round(sum(float(r[c]) for r in fact_rows), 6) for c in measure_cols}
    naive_totals = {c: round(sum(row[c] for row in result), 6) for c in measure_cols}

    # 丢失分析：哪些事实行在最终连接结果中完全消失（孤儿行），各丢了多少度量
    surviving = set()
    for r in joined:
        fact_ref = dict(next(p for t, p in r[PROV] if t == spec["fact_table"]))
        surviving.add(tuple(fact_ref[k] for k in fact_pk))
    lost_rows = []
    lost_totals = {c: 0.0 for c in measure_cols}
    for fr in fact_rows:
        fid = tuple(fr[k] for k in fact_pk)
        if fid not in surviving:
            lost_rows.append({"fact_key": list(fid),
                              "measures": {c: round(float(fr[c]), 6) for c in measure_cols}})
            for c in measure_cols:
                lost_totals[c] += float(fr[c])
    lost_totals = {c: round(v, 6) for c, v in lost_totals.items()}

    # 重复放大（只统计被复制行带来的净增量，剔除丢失干扰）
    repeated_extra = {c: 0.0 for c in measure_cols}
    for d in duplicates:
        for c, bucket in d["measures"].items():
            repeated_extra[c] += bucket["repeated_total"] - bucket["single_value"]
    repeated_extra = {c: round(v, 6) for c, v in repeated_extra.items()}

    return {
        "path": PATH_NAIVE,
        "eligible": True,
        "result": result,
        "stage_counts": stage_counts,
        "base_totals": base_totals,
        "naive_totals": naive_totals,
        "net_delta": {c: round(naive_totals[c] - base_totals[c], 6) for c in measure_cols},
        "repeated_extra": repeated_extra,   # 重复贡献导致的放大（正向）
        "lost_totals": lost_totals,         # 孤儿行导致的丢失（正向数值表示丢失量）
        "lost_rows": lost_rows,
        # 兼容旧字段名
        "inflation": {c: round(naive_totals[c] - base_totals[c], 6) for c in measure_cols},
        "duplicate_contributions": duplicates,
        "warnings": orphans,
    }


# ---------------------------------------------------------------------------
# ATJ：先聚合再关联
# ---------------------------------------------------------------------------
def check_atj(conn, spec, edges, measure_meta, plan_errors):
    reasons = list(plan_errors)
    for m in measure_meta:
        if not m["additive"]:
            reasons.append({
                "code": "NON_ADDITIVE_MEASURE",
                "detail": f"度量 {m['col']} 在登记中不可加（additive=0），不能先聚合再关联",
            })
    for edge in edges:
        if edge.fanout:
            reasons.append({
                "code": "FANOUT_EDGE",
                "edge": edge.rel["name"],
                "detail": f"边 {edge.rel['name']}（{edge.from_table}->{edge.to_table}）"
                          f"基数为 {edge.rel['cardinality']}{'（反向遍历）' if edge.reverse else ''}，"
                          f"会复制事实行，先聚合再关联不成立，应改用带分摊基准的 DJA",
            })
        else:
            reasons.extend(_check_uniqueness(conn, edge))
    return _dedupe_reasons(reasons)


def run_atj(conn, spec, edges, measure_meta):
    # 合法路径上全是 N:1 且一端唯一：逐表查找连接，行数必须保持不变，再聚合
    fact_rows = _load_table(conn, spec["fact_table"])
    stage_counts = [{"stage": f"事实表 {spec['fact_table']}", "rows": len(fact_rows)}]
    current = fact_rows
    for edge in edges:
        right_rows = _load_table(conn, edge.to_table)
        before = len(current)
        joined, _, _ = hash_join(current, right_rows, edge.left_cols, edge.right_cols)
        assert len(joined) == before, "ATJ 合法路径上连接不应改变行数，规划校验存在缺陷"
        stage_counts.append({"stage": f"查找关联 {edge.to_table}（行数不变）", "rows": len(joined)})
        current = joined
    measure_cols = [m["col"] for m in measure_meta]
    result = _group_sum(current, spec["group_by"], measure_cols)
    # ATJ 合法路径行数不变，合计必须等于事实表基准合计（再用断言锁死该不变量）
    base_totals = {c: round(sum(float(r[c]) for r in fact_rows), 6) for c in measure_cols}
    atj_totals = {c: round(sum(row[c] for row in result), 6) for c in measure_cols}
    assert all(abs(base_totals[c] - atj_totals[c]) <= TOL for c in measure_cols), \
        "ATJ 合法路径聚合合计偏离事实表基准，行数不变性被破坏"
    return {"path": PATH_ATJ, "eligible": True, "result": result,
            "stage_counts": stage_counts, "base_totals": base_totals,
            "atj_totals": atj_totals}


# ---------------------------------------------------------------------------
# DJA：明细连接后按贡献归并
# ---------------------------------------------------------------------------
def check_dja(conn, spec, edges, measure_meta, plan_errors):
    reasons = list(plan_errors)
    for m in measure_meta:
        if not m["additive"]:
            reasons.append({
                "code": "NON_ADDITIVE_MEASURE",
                "detail": f"度量 {m['col']} 不可加，无法按贡献归并",
            })
    for edge in edges:
        if edge.fanout:
            reasons.extend(_check_basis_completeness(conn, edge))
        else:
            reasons.extend(_check_uniqueness(conn, edge))
    return _dedupe_reasons(reasons)


def _dedupe_reasons(reasons: list[dict]) -> list[dict]:
    """桥接展开会产生两跳，同一关系的同码原因只保留首条（合并提示）。"""
    seen = set()
    out = []
    for r in reasons:
        key = (r["code"], r.get("edge"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def run_dja(conn, spec, edges, measure_meta):
    joined, stage_counts, orphans = _connect_path(conn, spec, edges, apply_factor=True)
    measure_cols = [m["col"] for m in measure_meta]
    result = _group_sum(joined, spec["group_by"], measure_cols, weighted=True)

    # 归并账本：每个事实行被折算后的贡献，证明总量守恒（权重完整时）
    fact_pk = next(m["pk_cols"] for m in store.list_table_meta(conn)
                   if m["table_name"] == spec["fact_table"])
    ledger = defaultdict(lambda: {"copies": 0, "weighted_sum": 0.0,
                                  "raw_sum": 0.0, "weight_sum": 0.0})
    for r in joined:
        fact_ref = dict(next(p for t, p in r[PROV] if t == spec["fact_table"]))
        fid = tuple(fact_ref[k] for k in fact_pk)
        e = ledger[fid]
        e["copies"] += 1
        e["weight_sum"] += r[FACTOR]
        for c in measure_cols:
            e["weighted_sum"] += float(r[c]) * r[FACTOR]
            e["raw_sum"] += float(r[c])
    contributions = [{
        "fact_pk": fact_pk, "fact_key": list(fid),
        "copies": e["copies"],
        "weight_sum": round(e["weight_sum"], 6),
        "raw_measure_total": round(e["raw_sum"], 6),
        "allocated_measure_total": round(e["weighted_sum"], 6),
    } for fid, e in sorted(ledger.items())]
    fact_rows = _load_table(conn, spec["fact_table"])
    base_totals = {c: round(sum(float(r[c]) for r in fact_rows), 6) for c in measure_cols}
    dja_totals = {c: round(sum(row[c] for row in result), 6) for c in measure_cols}
    return {
        "path": PATH_DJA, "eligible": True, "result": result,
        "stage_counts": stage_counts,
        "base_totals": base_totals,
        "allocated_totals": dja_totals,
        "contributions": contributions,
        "conservation_check": {c: abs(base_totals[c] - dja_totals[c]) <= TOL
                               for c in measure_cols},
        "warnings": orphans,
    }


# ---------------------------------------------------------------------------
# 总入口：一次运行三条路径 + 差异分析
# ---------------------------------------------------------------------------
def run_report(conn, spec: dict) -> dict:
    _validate_spec(spec)
    measure_meta = _measure_meta(conn, spec)
    edges, plan_errors = plan_path(conn, spec)

    # NAIVE 始终执行（即使规划有问题也尽力而为，用于暴露症状）
    try:
        naive = run_naive(conn, spec, edges, measure_meta) if not plan_errors else {
            "path": PATH_NAIVE, "eligible": False, "reasons": plan_errors}
    except EngineError as exc:
        naive = {"path": PATH_NAIVE, "eligible": False,
                 "reasons": [{"code": "ENGINE_ERROR", "detail": str(exc)}]}

    atj_reasons = check_atj(conn, spec, edges, measure_meta, plan_errors)
    dja_reasons = check_dja(conn, spec, edges, measure_meta, plan_errors)

    atj = ({"path": PATH_ATJ, "eligible": False, "reasons": atj_reasons}
           if atj_reasons else run_atj(conn, spec, edges, measure_meta))
    dja = ({"path": PATH_DJA, "eligible": False, "reasons": dja_reasons}
           if dja_reasons else run_dja(conn, spec, edges, measure_meta))

    diagnosis = build_diagnosis(spec, naive, atj, dja, edges)
    return {"spec": spec, "edges": [_edge_view(e) for e in edges],
            "naive": naive, "atj": atj, "dja": dja, "diagnosis": diagnosis}


def _edge_view(edge: Edge) -> dict:
    return {"relationship": edge.rel["name"], "from": edge.from_table,
            "to": edge.to_table, "left_cols": edge.left_cols,
            "right_cols": edge.right_cols, "fanout": edge.fanout,
            "factor_col": edge.factor_col, "reverse": edge.reverse}


def _validate_spec(spec):
    for key in ("report_name", "fact_table", "measures", "group_by"):
        if key not in spec:
            raise EngineError(f"报表定义缺少字段 {key}")
    if not isinstance(spec["measures"], list) or not spec["measures"]:
        raise EngineError("measures 必须是非空数组")
    if not isinstance(spec["group_by"], list):
        raise EngineError("group_by 必须是数组")


def build_diagnosis(spec, naive, atj, dja, edges) -> dict:
    """差异分析：判定症状、定位重复贡献、给出可重建路径。"""
    findings = []
    valid_paths = [p["path"] for p in (atj, dja) if p.get("eligible")]

    if naive.get("eligible"):
        repeated = {c: v for c, v in naive.get("repeated_extra", {}).items() if abs(v) > TOL}
        lost = {c: v for c, v in naive.get("lost_totals", {}).items() if abs(v) > TOL}
        net = {c: v for c, v in naive["inflation"].items() if abs(v) > TOL}
        if repeated:
            findings.append({
                "severity": "ERROR",
                "code": "DUPLICATE_CONTRIBUTION",
                "summary": f"连接扇出使事实行被复制，重复贡献净增 {repeated}（金额/计数被放大），"
                           f"逐行来源见重复贡献账本",
            })
        if lost:
            findings.append({
                "severity": "ERROR",
                "code": "ORPHAN_LOSS",
                "summary": f"内连接丢弃了无匹配的事实行，度量被静默吞掉 {lost}；"
                           f"净差异 {net} 是'重复放大'与'孤儿丢失'相互抵消后的结果，"
                           f"净额正常不代表关联正确",
            })
        if not repeated and not lost:
            findings.append({
                "severity": "OK",
                "code": "NAIVE_OK",
                "summary": "本查询路径无扇出、无孤儿行，朴素连接未放大或丢失度量",
            })
        for w in naive.get("warnings", []):
            findings.append({"severity": "WARN", "code": w["code"], "summary": w["detail"]})

    for path_obj, label in ((atj, "先聚合再关联"), (dja, "明细连接按贡献归并")):
        if not path_obj.get("eligible"):
            for reason in path_obj["reasons"]:
                findings.append({
                    "severity": "REJECT",
                    "code": reason["code"],
                    "path": path_obj["path"],
                    "summary": f"{label}被拒绝：{reason['detail']}",
                })

    # 正确值与朴素值的分组级对照
    reference = next((p for p in (atj, dja) if p.get("eligible")), None)
    group_diffs = []
    if reference and naive.get("eligible"):
        ref_index = {tuple(r["group"]): r for r in reference["result"]}
        measure_cols = [m["col"] for m in spec["measures"]]
        for nrow in naive["result"]:
            key = tuple(nrow["group"])
            rrow = ref_index.get(key)
            if rrow is None:
                continue
            for c in measure_cols:
                delta = round(nrow[c] - rrow[c], 6)
                if abs(delta) > TOL:
                    group_diffs.append({
                        "group": list(key), "measure": c,
                        "naive_value": nrow[c], "correct_value": rrow[c],
                        "delta": delta,
                        "amplification_ratio": round(nrow[c] / rrow[c], 4)
                        if rrow[c] else None,
                    })

    rebuildable = bool(valid_paths)
    return {
        "findings": findings,
        "valid_paths": valid_paths,
        "rebuildable": rebuildable,
        "group_level_differences": group_diffs,
        "conclusion": _conclusion(findings, valid_paths),
    }


def _conclusion(findings, valid_paths) -> str:
    codes = {f["code"] for f in findings}
    has_fanout_problem = bool(codes & {"DUPLICATE_CONTRIBUTION", "ORPHAN_LOSS"})
    has_reject = any(f["severity"] == "REJECT" for f in findings)
    if has_fanout_problem and valid_paths:
        return (f"检测到错误关联（重复放大/孤儿丢失）；{','.join(valid_paths)} 路径合法，"
                f"应使用其结果重建报表，禁止对朴素结果做事后去重")
    if has_fanout_problem and not valid_paths:
        return ("检测到错误关联且当前登记下没有任何合法计算路径：必须先在粒度登记中"
                "修正关系基数或补登分摊基准，再重建报表")
    if has_reject:
        return "查询被合法路径拒绝，请按拒绝原因修正登记或改用更细粒度的事实表"
    if valid_paths:
        return f"查询合法，可使用 {','.join(valid_paths)} 路径，朴素连接与正确路径结果一致"
    return "查询无法在当前登记下加工，详见拒绝原因"
