"""两事件耗时约束判定的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_duration_bounds.py

每组用例都独立验证：
- 端点时刻见证落在每个事件的闭区间内；
- 全部约束（含 source_order）被满足（允许同刻）；
- 见证算出的 end−start 恰等于返回的 min/max；
- 朴素"区间直接相减"会给出更宽（错误）的界。
"""
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-dur-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app.duration_bounds import duration_bounds, DurationError  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")

EPS = 1e-5


def load_sample():
    reset_all()
    with open(EXAMPLE, encoding="utf-8") as f:
        import_payload(json.load(f))


def import_case(sources, events, constraints=None):
    reset_all()
    import_payload({
        "sources": sources,
        "events": events,
        "constraints": constraints or [],
    })


def validate_witness(r, which):
    """见证必须是合法的全量时间分配，且达到声称的端点值。"""
    w = r["witnesses"][which]
    ev = {e["id"]: e for e in w["events"]}
    # 闭区间
    for e in w["events"]:
        assert e["lo"] <= e["time"] + EPS, (which, e["id"], e["lo"], e["time"])
        assert e["time"] <= e["hi"] + EPS, (which, e["id"], e["time"], e["hi"])
        assert e["slack_to_lo"] >= -EPS and e["slack_to_hi"] >= -EPS
    # 差值恰为声称端点
    got = ev[r["end"]]["time"] - ev[r["start"]]["time"]
    assert abs(got - w["value_seconds"]) < EPS, (which, got, w["value_seconds"])
    target = r["duration"]["min_seconds" if which == "min" else "max_seconds"]
    assert abs(got - target) < EPS, (which, got, target)
    return got


def validate_edges(r, raw_edges):
    """全部原始约束在两组见证中都成立（t_to ≥ t_from）。"""
    for which in ("min", "max"):
        ev = {e["id"]: e for e in r["witnesses"][which]["events"]}
        for e in raw_edges:
            assert ev[e["to"]]["time"] + EPS >= ev[e["from"]]["time"], (
                which, e["from"], e["to"])


class BasicBounds(unittest.TestCase):

    def test_two_unconnected_events_full_intervals(self):
        # 无约束、无 source_order（不同来源各一条）：紧界就是区间差
        import_case(
            [{"id": "s1", "uncertainty": 1}, {"id": "s2", "uncertainty": 2}],
            [{"id": "A", "source": "s1", "time": 100},   # [99,101]
             {"id": "B", "source": "s2", "time": 110}],  # [108,112]
        )
        r = duration_bounds("A", "B", 5.0)
        self.assertTrue(r["feasible"])
        self.assertAlmostEqual(r["duration"]["min_seconds"], 7.0, places=4)
        self.assertAlmostEqual(r["duration"]["max_seconds"], 13.0, places=4)
        n = r["naive_interval_subtraction"]
        self.assertTrue(n["matches"])
        validate_witness(r, "min")
        validate_witness(r, "max")
        # 7 ≤ 5 不成立 → 必超
        self.assertEqual(r["verdict"], "must_exceed")

    def test_order_edge_clamps_min_to_zero(self):
        # 朴素区间差给出负下界，但 A→B 约束把最小耗时锁为 0
        import_case(
            [{"id": "s1", "uncertainty": 5}, {"id": "s2", "uncertainty": 5}],
            [{"id": "A", "source": "s1", "time": 100},   # [95,105]
             {"id": "B", "source": "s2", "time": 104}],  # [99,109]
            [{"from": "A", "to": "B"}],
        )
        r = duration_bounds("A", "B", 10.0)
        self.assertAlmostEqual(r["duration"]["min_seconds"], 0.0, places=4)
        self.assertAlmostEqual(r["duration"]["max_seconds"], 14.0, places=4)
        self.assertTrue(r["reachability"]["start_reaches_end"])
        self.assertFalse(r["reachability"]["end_reaches_start"])
        n = r["naive_interval_subtraction"]
        self.assertAlmostEqual(n["lo_seconds"], -6.0, places=4)  # 朴素下界错
        self.assertFalse(n["matches"])
        self.assertEqual(r["verdict"], "may_exceed")  # [0,14] 跨 10
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "A", "to": "B"}])

    def test_reverse_edge_negative_duration(self):
        # B 实际必早于 A（边 B→A）：问 A 到 B 的耗时，界恒为负
        import_case(
            [{"id": "s1", "uncertainty": 1}, {"id": "s2", "uncertainty": 1}],
            [{"id": "A", "source": "s1", "time": 105},   # [104,106]
             {"id": "B", "source": "s2", "time": 100}],  # [99,101]
            [{"from": "B", "to": "A"}],
        )
        r = duration_bounds("A", "B", 0.0)
        self.assertAlmostEqual(r["duration"]["max_seconds"], -3.0, places=4)
        self.assertAlmostEqual(r["duration"]["min_seconds"], -7.0, places=4)
        self.assertTrue(r["reachability"]["end_reaches_start"])
        # 阈值 0：最大耗时 -3 ≤ 0 → 必不超（终点必早于起点，不存在超阈值安排）
        self.assertEqual(r["verdict"], "must_not_exceed")
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "B", "to": "A"}])

    def test_chain_path_propagates(self):
        # A→B→C，仅问 A→C：朴素忽略路径，路径 + 区间把下界抬起
        import_case(
            [{"id": "s1", "uncertainty": 5},
             {"id": "s2", "uncertainty": 5},
             {"id": "s3", "uncertainty": 2}],
            [{"id": "A", "source": "s1", "time": 100},   # [95,105]
             {"id": "B", "source": "s2", "time": 104},   # [99,109]
             {"id": "C", "source": "s3", "time": 110}],  # [108,112]
            [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
        )
        r = duration_bounds("A", "C", 20.0)
        # C[A] = min(hi_A=105, hi_B=109, hi_C=112)=105；H[C]=max(lo)=108 → min=3
        self.assertAlmostEqual(r["duration"]["min_seconds"], 3.0, places=4)
        # C[C]=112；H[A]=95 → max=17
        self.assertAlmostEqual(r["duration"]["max_seconds"], 17.0, places=4)
        n = r["naive_interval_subtraction"]
        self.assertAlmostEqual(n["lo_seconds"], 3.0, places=4)   # 巧合下界相同
        self.assertAlmostEqual(n["hi_seconds"], 17.0, places=4)
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}])

    def test_cycle_pins_same_time(self):
        import_case(
            [{"id": "s1", "uncertainty": 10}, {"id": "s2", "uncertainty": 10}],
            [{"id": "x", "source": "s1", "time": 100},   # [90,110]
             {"id": "y", "source": "s2", "time": 105}],  # [95,115]
            [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}],
        )
        r = duration_bounds("x", "y", 1.0)
        self.assertTrue(r["reachability"]["same_component"])
        self.assertAlmostEqual(r["duration"]["min_seconds"], 0.0, places=4)
        self.assertAlmostEqual(r["duration"]["max_seconds"], 0.0, places=4)
        self.assertEqual(r["verdict"], "must_not_exceed")
        wmin = {e["id"]: e for e in r["witnesses"]["min"]["events"]}
        self.assertAlmostEqual(wmin["x"]["time"], wmin["y"]["time"], places=4)
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}])

    def test_disconnected_components_independent(self):
        # A,B 在同一有约束分支；X 与它们完全无关：界只由各自区间决定
        import_case(
            [{"id": "s1", "uncertainty": 1},
             {"id": "s2", "uncertainty": 1},
             {"id": "s3", "uncertainty": 1}],
            [{"id": "A", "source": "s1", "time": 100},
             {"id": "B", "source": "s2", "time": 101},
             {"id": "X", "source": "s3", "time": 200}],
            [{"from": "A", "to": "B"}],
        )
        r = duration_bounds("A", "X", 1000.0)
        # A:[99,101] X:[199,201] → [98,102]
        self.assertAlmostEqual(r["duration"]["min_seconds"], 98.0, places=4)
        self.assertAlmostEqual(r["duration"]["max_seconds"], 102.0, places=4)
        desc = r["reachability"]["description"]
        self.assertIn("没有任何方向", desc)
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "A", "to": "B"}])


class VerdictThresholds(unittest.TestCase):

    def setUp(self):
        import_case(
            [{"id": "s1", "uncertainty": 2}, {"id": "s2", "uncertainty": 2}],
            [{"id": "A", "source": "s1", "time": 100},   # [98,102]
             {"id": "B", "source": "s2", "time": 110}],  # [108,112]
        )  # 区间 [6,14]，无连接路径

    def test_must_exceed(self):
        r = duration_bounds("A", "B", 5.0)
        self.assertEqual(r["verdict"], "must_exceed")
        self.assertEqual(r["verdict_text"], "必超")

    def test_must_not_exceed(self):
        r = duration_bounds("A", "B", 14.0)   # 端点相等：max ≤ threshold
        self.assertEqual(r["verdict"], "must_not_exceed")
        r2 = duration_bounds("A", "B", 20.0)
        self.assertEqual(r2["verdict"], "must_not_exceed")

    def test_may_exceed(self):
        r = duration_bounds("A", "B", 6.0)    # 端点相等：min > 6 不成立
        self.assertEqual(r["verdict"], "may_exceed")
        r2 = duration_bounds("A", "B", 10.0)
        self.assertEqual(r2["verdict"], "may_exceed")
        r3 = duration_bounds("A", "B", 13.999)
        self.assertEqual(r3["verdict"], "may_exceed")


class ThirdPartyCoupling(unittest.TestCase):
    """朴素区间相减出错、但第三方事件区间通过路径收紧界的情形。"""

    def test_intermediate_interval_tightens(self):
        # A:[95,105] B:[99,109] C:[106,108]（半径 1）
        # A→B→C：问 A→C。朴素 [1,13]；C[A]=105 → H[C]=106 → min=1（同），
        # 但问 B→C：朴素 [-3,13]，约束 B→C 锁 min=0，且 hi_C=108 限 max=9。
        import_case(
            [{"id": "s1", "uncertainty": 5},
             {"id": "s2", "uncertainty": 5},
             {"id": "s3", "uncertainty": 1}],
            [{"id": "A", "source": "s1", "time": 100},
             {"id": "B", "source": "s2", "time": 104},
             {"id": "C", "source": "s3", "time": 107}],
            [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
        )
        r = duration_bounds("B", "C", 5.0)
        self.assertAlmostEqual(r["duration"]["min_seconds"], 0.0, places=4)
        self.assertAlmostEqual(r["duration"]["max_seconds"], 9.0, places=4)
        n = r["naive_interval_subtraction"]
        self.assertAlmostEqual(n["lo_seconds"], -3.0, places=4)
        self.assertAlmostEqual(n["hi_seconds"], 9.0, places=4)
        self.assertFalse(n["matches"])
        validate_witness(r, "min")
        validate_witness(r, "max")
        validate_edges(r, [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}])


class SampleAndInfeasible(unittest.TestCase):

    def test_sample_feasible_after_correction_witness(self):
        load_sample()
        r = duration_bounds("g1", "a3", 120.0,
                            {"app-server": {"clock_offset": 50},
                             "mobile-app": {"clock_offset": -30}})
        self.assertTrue(r["feasible"])
        self.assertIn("duration", r)
        dmin = r["duration"]["min_seconds"]
        dmax = r["duration"]["max_seconds"]
        self.assertLessEqual(dmin, dmax + EPS)
        validate_witness(r, "min")
        validate_witness(r, "max")
        with open(EXAMPLE, encoding="utf-8") as f:
            raw_edges = json.load(f)["constraints"]
        validate_edges(r, raw_edges)
        # g1⇝a3 沿因果链可达 → 最小耗时非负
        self.assertTrue(r["reachability"]["start_reaches_end"])
        self.assertGreaterEqual(dmin, -EPS)

    def test_global_infeasible_returns_conflict_witness_no_duration(self):
        load_sample()  # 默认假设下 a1→m1 单独即不可能，全局无解
        r = duration_bounds("a1", "m1", 10.0)
        self.assertFalse(r["feasible"])
        self.assertNotIn("duration", r)
        self.assertNotIn("verdict", r)
        w = r["witness"]
        self.assertEqual(w["head"]["event"], "a1")
        self.assertEqual(w["tail"]["event"], "m1")
        self.assertIn("steps", w)

    def test_overrides_not_persisted(self):
        load_sample()
        before = duration_bounds("g1", "g2", 1000.0)
        self.assertFalse(before["feasible"])  # 默认假设即无解
        from app.db import db
        with db() as conn:
            off = conn.execute(
                "SELECT clock_offset FROM sources WHERE id='app-server'"
            ).fetchone()["clock_offset"]
        self.assertEqual(off, 0.0)


class Validation(unittest.TestCase):

    def setUp(self):
        load_sample()

    def test_same_event_rejected(self):
        with self.assertRaises(DurationError):
            duration_bounds("g1", "g1", 1.0)

    def test_unknown_event_rejected(self):
        with self.assertRaises(DurationError) as cm:
            duration_bounds("g1", "nope", 1.0)
        self.assertIn("不存在", str(cm.exception))
        with self.assertRaises(DurationError):
            duration_bounds("nope", "g1", 1.0)

    def test_blank_event_rejected(self):
        with self.assertRaises(DurationError):
            duration_bounds("  ", "g1", 1.0)
        with self.assertRaises(DurationError):
            duration_bounds("g1", "", 1.0)

    def test_bad_threshold(self):
        for bad in (-0.01, -1, float("inf"), float("-inf"), float("nan"), "10", None):
            with self.assertRaises(DurationError):
                duration_bounds("g1", "g2", bad)

    def test_bool_threshold_rejected(self):
        with self.assertRaises(DurationError):
            duration_bounds("g1", "g2", True)

    def test_zero_threshold_allowed(self):
        r = duration_bounds("g1", "g2", 0.0,
                            {"app-server": {"clock_offset": 50},
                             "mobile-app": {"clock_offset": -30}})
        self.assertTrue(r["feasible"])
        self.assertEqual(r["threshold_seconds"], 0.0)


class WitnessTightness(unittest.TestCase):
    """蒙特卡洛式交叉验证：随机小系统上界不被任何随机可行分配突破。"""

    def test_bounds_are_tight_on_random_systems(self):
        import random
        random.seed(20260929)
        for trial in range(60):
            n = random.randint(2, 7)
            sources = [{"id": f"s{i}", "uncertainty": round(random.uniform(0.2, 4), 2)}
                       for i in range(n)]
            t0 = 1000.0 + trial
            events = [{"id": f"e{i}", "source": f"s{i}",
                       "time": round(t0 + random.uniform(-8, 8), 3)}
                      for i in range(n)]
            edges = []
            for i in range(n):
                for j in range(i + 1, n):
                    if random.random() < 0.28:
                        edges.append({"from": f"e{i}", "to": f"e{j}"})
            import_case(sources, events, edges)
            try:
                r = duration_bounds("e0", f"e{n-1}", 3.0)
            except DurationError:
                continue
            if not r["feasible"]:
                continue
            dmin, dmax = r["duration"]["min_seconds"], r["duration"]["max_seconds"]
            validate_witness(r, "min")
            validate_witness(r, "max")
            validate_edges(r, edges)
            self.assertLessEqual(dmin, dmax + EPS)
            # 随机采样可行分配：从区间独立抽样后不满足边就做"上抬"闭包
            los = [1000.0] * n  # 占位，下面从结果见证反推区间
            evmin = {e["id"]: e for e in r["witnesses"]["min"]["events"]}
            lo = [evmin[f"e{i}"]["lo"] for i in range(n)]
            hi = [evmin[f"e{i}"]["hi"] for i in range(n)]
            adj = [[] for _ in range(n)]
            id2i = {f"e{i}": i for i in range(n)}
            for e in edges:
                adj[id2i[e["from"]]].append(id2i[e["to"]])
            for _sample in range(80):
                t = [random.uniform(lo[i], hi[i]) for i in range(n)]
                # 迭代闭包：t[v] = max(t[v], t[u])，若越过 hi 则该样本不可行
                for _it in range(n):
                    changed = False
                    for u in range(n):
                        for v in adj[u]:
                            if t[v] < t[u]:
                                t[v] = t[u]
                                changed = True
                    if not changed:
                        break
                if all(t[i] <= hi[i] + EPS for i in range(n)):
                    diff = t[n - 1] - t[0]
                    self.assertGreaterEqual(diff, dmin - 1e-3, (trial, diff, dmin))
                    self.assertLessEqual(diff, dmax + 1e-3, (trial, diff, dmax))


if __name__ == "__main__":
    unittest.main(verbosity=2)
