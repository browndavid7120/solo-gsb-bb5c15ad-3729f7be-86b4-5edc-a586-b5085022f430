"""两事件耗时的证据约束界：在全部可行时间分配中求 t_end − t_start 的精确最小/最大值。

定位
----
``feasibility`` 回答"全量区间 × 全部约束联立是否可行"；本模块在**同一套
差分约束系统**上回答审计员的下一个问题：**两个指定事件之间的耗时
``t_end − t_start``，被证据约束到什么范围？**

模型与 ``feasibility`` 完全一致（复用其求解器，含未保存时钟假设）：

    lo_i ≤ t_i ≤ hi_i          （每个事件的真实时间闭区间）
    t_b − t_a ≥ 0              （每条有效约束边 a→b，允许同刻）

不是把两事件各自区间直接相减——区间差 ``[lo_end−hi_start, hi_end−lo_start]``
忽略了两事件之间的约束路径与第三方事件区间的耦合（典型：一条 a→b 边配合
重叠区间可把最小耗时锁成 0，环则把两端钉在同一时刻）。

极值（差分约束系统的最长路定理）
--------------------------------
设约束图含虚节点 0：边 0→i 权 lo_i，边 i→0 权 −hi_i，事件边权 0。
可行 ⟺ 无正环。任意两事件差的紧界即虚图最长路：

    min(t_v − t_u) = 最长路 u→v 的权
                   = max( 0（若存在事件路径 u⇝v）,
                          H[v] − C[u] )
    max(t_v − t_u) = − 最长路 v→u 的权
                   = min( 0（若存在事件路径 v⇝u）,
                          C[v] − H[u] )

其中（都在 SCC 压缩 DAG 上传播，环内成员取值一致）：

    H[i] = 最早可行时刻 = max{lo_j : j⇝i}（即可行性求解器的 comp_value）
    C[i] = min{hi_k : i⇝k}（自身及全部后继的最小区间上界，反向传播）

端点见证（闭式构造，无需再次求解）
----------------------------------
达到最小值（root=u，把 u 及其后继整体抬到不违反任何上界的位置）：

    t_i = max(H[i], C[u])   若 i 是 u 的后继；否则 t_i = H[i]

达到最大值（把 u 及其祖先整体压低）：

    t_i = min(C[i], H[u])   若 i 是 u 的祖先；否则 t_i = C[i]

可逐边、逐区间验证（单调性由 H/C 在 DAG 上的传播序保证；环内取同刻）。

全程只读：不写数据库、不保存假设、不改写事件；不受页面筛选影响（全量联立）。
全局无解时复用 ``feasibility`` 的矛盾路径见证，不给出任何耗时结论。
"""
from __future__ import annotations

from typing import Any

from .feasibility import _Solver
from .timeutil import fmt, fmt_delta

EPS = 1e-6


class DurationError(ValueError):
    """输入校验错误（消息含"不存在"时路由按 404，否则 400）。"""


# ------------------------------------------------------------ SCC DAG 传播

class _Bounds:
    """一次耗时界计算的图结构（在可行性求解器结果上做 DAG 传播）。"""

    def __init__(self, solver: _Solver):
        self.s = solver
        m = len(solver.comps)
        self.m = m
        self.cu_of = solver.comp_of
        self.ids = solver.ids

        # 压缩 DAG 邻接（去重平行边：可达性/极值传播只需分量间有无边）
        self.succ: list[set[int]] = [set() for _ in range(m)]
        for e in solver.cross_edges:
            self.succ[e["cu"]].add(e["cv"])
        self.pred: list[set[int]] = [set() for _ in range(m)]
        for c in range(m):
            for q in self.succ[c]:
                self.pred[q].add(c)

        # Kahn 拓扑序（迭代）
        indeg = [len(self.pred[c]) for c in range(m)]
        queue = [c for c in range(m) if indeg[c] == 0]
        topo: list[int] = []
        head = 0
        while head < len(queue):
            c = queue[head]
            head += 1
            topo.append(c)
            for q in self.succ[c]:
                indeg[q] -= 1
                if indeg[q] == 0:
                    queue.append(q)
        self.topo = topo

        # 可达位集（含自身）：
        # desc[c] = c 能到达的分量，逆拓扑（后继已算完）向自身并入；
        # anc[c]  = 能到达 c 的分量，正拓扑（前驱已算完）向自身并入。
        desc = [1 << c for c in range(m)]
        for c in reversed(topo):
            bits = desc[c]
            for q in self.succ[c]:
                bits |= desc[q]
            desc[c] = bits
        self.desc = desc
        anc = [1 << c for c in range(m)]
        for c in topo:
            bits = anc[c]
            for p in self.pred[c]:
                bits |= anc[p]
            anc[c] = bits
        self.anc = anc

        # H[c]：最早可行时刻（直接复用可行性求解器的最长路结果）
        self.H = list(solver.comp_value)
        # C[c]：自身与全部后继分量的最小 min(hi)，逆拓扑传播
        cap = list(solver.comp_min_hi)
        for c in reversed(topo):
            best = cap[c]
            for q in self.succ[c]:
                if cap[q] < best:
                    best = cap[q]
            cap[c] = best
        self.C = cap

    def reaches(self, a: int, b: int) -> bool:
        return bool(self.desc[a] & (1 << b))


# ------------------------------------------------------------ 见证组装

def _witness_events(solver: _Solver, times: list[float]) -> list[dict[str, Any]]:
    """把一组分量级时刻展开为全量事件见证（逐事件给出区间与两侧余量）。"""
    out = []
    for i, node in enumerate(solver.nodes):
        c = solver.comp_of[i]
        t = times[c]
        out.append({
            "id": solver.ids[i],
            "source_id": node.raw["source_id"],
            "summary": node.raw.get("summary", ""),
            "time": round(t, 6),
            "time_text": fmt(t),
            "lo": round(solver.lo[i], 6),
            "hi": round(solver.hi[i], 6),
            "lo_text": fmt(solver.lo[i]),
            "hi_text": fmt(solver.hi[i]),
            # 见证自证：取值落在闭区间内（两侧余量都 ≥ 0）
            "slack_to_lo": round(t - solver.lo[i], 6),
            "slack_to_hi": round(solver.hi[i] - t, 6),
            "in_cycle": len(solver.comps[c]) > 1,
        })
    out.sort(key=lambda e: (e["time"], e["id"]))
    return out


def _min_assignment(b: _Bounds, root: int) -> list[float]:
    """达到 min(t_end − t_start) 的全量时刻：root 后继整体抬到 C[root]。"""
    s = b.s
    reach = b.desc[root]
    raise_to = b.C[root]
    times = []
    for c in range(b.m):
        if reach & (1 << c):
            t = b.H[c] if b.H[c] > raise_to + EPS else raise_to
        else:
            t = b.H[c]
        times.append(t)
    return times


def _max_assignment(b: _Bounds, root: int) -> list[float]:
    """达到 max(t_end − t_start) 的全量时刻：root 祖先整体压到 H[root]。"""
    reach = b.anc[root]
    lower_to = b.H[root]
    times = []
    for c in range(b.m):
        if reach & (1 << c):
            t = b.C[c] if b.C[c] < lower_to - EPS else lower_to
        else:
            t = b.C[c]
        times.append(t)
    return times


# ------------------------------------------------------------ 阈值校验

def _validate_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # 放行 float/int（JSON number）；字符串等类型由调用方在路由层转拒
        raise DurationError("耗时阈值必须是有限的非负数值（秒）")
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        raise DurationError("耗时阈值必须是有限数值，不能为 NaN / Infinity")
    if f < -EPS:
        raise DurationError("耗时阈值必须是非负数（秒），不能为负")
    return max(0.0, f)


# ------------------------------------------------------------ 主入口

def duration_bounds(start_id: str, end_id: str, threshold: Any,
                    overrides: dict | None = None) -> dict[str, Any]:
    """求 end − start 在全部可行时间分配中的精确最小/最大值与阈值判定。

    start_id/end_id 为事件 ID；threshold 为非负有限秒数。
    overrides 携带页面当前（含未保存）的时钟假设。只读，不落库任何数据。
    """
    if not isinstance(start_id, str) or not start_id.strip():
        raise DurationError("起点事件未指定")
    if not isinstance(end_id, str) or not end_id.strip():
        raise DurationError("终点事件未指定")
    start_id, end_id = start_id.strip(), end_id.strip()
    if start_id == end_id:
        raise DurationError(f"起点与终点必须是两个不同事件（收到的都是 {start_id}）")
    limit = _validate_threshold(threshold)

    solver = _Solver(overrides)
    u = solver.id_idx.get(start_id)
    v = solver.id_idx.get(end_id)
    if u is None or v is None:
        # 未知事件明确拒绝，优先于可行性判定（全局无解也不能吞掉输入错误）
        missing = start_id if u is None else end_id
        raise DurationError(f"事件不存在: {missing}")

    solver.solve()

    # 与 feasibility.result() 完全相同的无解判定与分量选择：取超限最严重者，
    # 保证交付的矛盾见证与"全局时序可行性"检查一致（即现有矛盾见证）。
    failed: int | None = None
    best_key: tuple[float, str] | None = None
    for ci in range(len(solver.comps)):
        overshoot = solver.comp_value[ci] - solver.comp_min_hi[ci]
        if overshoot <= EPS:
            continue
        key = (overshoot, solver.ids[solver.comp_min_hi_src[ci]])
        if best_key is None or key[0] > best_key[0] + EPS or (
                abs(key[0] - best_key[0]) <= EPS and key[1] < best_key[1]):
            best_key = key
            failed = ci

    base: dict[str, Any] = {
        "check_type": "event-duration-bounds",
        "start": start_id,
        "end": end_id,
        "threshold_seconds": round(limit, 6),
        "stats": {
            "event_count": solver.n,
            "constraint_count": len(solver.edges),
            "skipped_constraints": solver.skipped_edges,
            "component_count": len(solver.comps),
        },
    }

    if failed is not None:
        # 全局无解：只交付矛盾见证，不输出任何耗时结论
        base.update({
            "feasible": False,
            "summary": "当前时钟假设下，全量事件区间与全部约束联立无解，"
                       "耗时界不存在；请先参照下方矛盾路径调整假设或核对约束。",
            "witness": solver._witness(failed),
        })
        return base

    b = _Bounds(solver)
    cu, cv = b.cu_of[u], b.cu_of[v]
    u_to_v = b.reaches(cu, cv)
    v_to_u = b.reaches(cv, cu)

    path_term_min = b.H[cv] - b.C[cu]
    dmin = max(0.0, path_term_min) if u_to_v else path_term_min
    path_term_max = b.C[cv] - b.H[cu]
    dmax = min(0.0, path_term_max) if v_to_u else path_term_max

    min_times = _min_assignment(b, cu)
    max_times = _max_assignment(b, cu)

    # 朴素"区间直接相减"仅作对照展示，并明确标注其不是证据约束的结果
    naive_lo = solver.lo[v] - solver.hi[u]
    naive_hi = solver.hi[v] - solver.lo[u]

    if dmin - limit > EPS:
        verdict = "must_exceed"
        verdict_text = "必超"
        verdict_explain = (
            f"最小可能耗时 {fmt_delta(dmin)} 已大于阈值 {fmt_delta(limit)}："
            "无论怎样在证据范围内安排事件时刻，耗时都必然超过阈值。")
    elif dmax <= limit + EPS:
        verdict = "must_not_exceed"
        verdict_text = "必不超"
        verdict_explain = (
            f"最大可能耗时 {fmt_delta(dmax)} 不超过阈值 {fmt_delta(limit)}："
            "无论怎样在证据范围内安排事件时刻，耗时都必然不超过阈值。")
    else:
        verdict = "may_exceed"
        verdict_text = "可能超"
        verdict_explain = (
            f"耗时范围 [{fmt_delta(dmin)}, {fmt_delta(dmax)}] 跨越阈值 "
            f"{fmt_delta(limit)}：存在不超阈值的可行安排，也存在超过阈值的"
            "可行安排，现有证据不能唯一断定。")

    reach_text = []
    if u_to_v:
        reach_text.append(f"存在 {start_id} ⇝ {end_id} 的约束路径（耗时下界被锁为 ≥ 0）")
    if v_to_u:
        reach_text.append(f"存在 {end_id} ⇝ {start_id} 的约束路径（耗时上界被锁为 ≤ 0）")
    if not reach_text:
        reach_text.append("两事件之间没有任何方向的约束路径（若有第三方耦合仍会收紧界，"
                          "不等于各自区间直接相减）")
    if cu == cv:
        reach_text.append("两事件处于同一因果环，环内必须同刻，耗时恒为 0")

    base.update({
        "feasible": True,
        "duration": {
            "min_seconds": round(dmin, 6),
            "max_seconds": round(dmax, 6),
            "min_text": fmt_delta(dmin),
            "max_text": fmt_delta(dmax),
            "span_seconds": round(dmax - dmin, 6),
        },
        "reachability": {
            "start_reaches_end": u_to_v,
            "end_reaches_start": v_to_u,
            "same_component": cu == cv,
            "description": "；".join(reach_text),
        },
        "verdict": verdict,
        "verdict_text": verdict_text,
        "verdict_explanation": verdict_explain,
        "naive_interval_subtraction": {
            "lo_seconds": round(naive_lo, 6),
            "hi_seconds": round(naive_hi, 6),
            "lo_text": fmt_delta(naive_lo),
            "hi_text": fmt_delta(naive_hi),
            "note": "直接用两事件各自区间相减会忽略约束路径与第三方事件区间，"
                    "不是证据约束下的真实紧界，仅作对照",
            "matches": (abs(naive_lo - dmin) <= EPS and abs(naive_hi - dmax) <= EPS),
        },
        "witnesses": {
            "min": {
                "achieves": "min_seconds",
                "value_seconds": round(min_times[cv] - min_times[cu], 6),
                "note": "达到最小耗时的一种全量事件时刻安排：起点其后继被整体抬到"
                        "不违反任何区间上界的最早位置；每个事件均落在自身闭区间内、"
                        "满足全部约束（允许同刻）。",
                "events": _witness_events(solver, min_times),
            },
            "max": {
                "achieves": "max_seconds",
                "value_seconds": round(max_times[cv] - max_times[cu], 6),
                "note": "达到最大耗时的一种全量事件时刻安排：起点其祖先被整体压到"
                        "不违反任何区间下界的最晚位置；每个事件均落在自身闭区间内、"
                        "满足全部约束（允许同刻）。",
                "events": _witness_events(solver, max_times),
            },
        },
        "summary": (f"在全部可行时间分配中，{end_id} − {start_id} 的耗时精确范围为 "
                    f"[{fmt_delta(dmin)}, {fmt_delta(dmax)}]（可为负，负表示终点实际"
                    f"早于起点）；相对阈值 {fmt_delta(limit)} 的判定：{verdict_text}。"),
    })
    return base
