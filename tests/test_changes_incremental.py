#!/usr/bin/env python3
"""变化追踪增量计算与旧全量口径的等价性回归（纯 Python，无需数据库）。

对随机有效的前后断言集，比较：
- 新实现 app.changes.relation_diffs（候选对定位 + 精算）；
- 参考实现 reference_diffs（旧口径：对全部已登记地层逐对全量比较）；
两者输出必须**完全一致**（顺序、关系、最优路径）。

另含针对性场景（同一期合并 / 拆分、路径改道、平行断言撤回、跨分量接通、
晚于方向、无净变更）与性能冒烟（大量无关地层时候选对应远小于全库对数）。

用法：python3 tests/test_changes_incremental.py
"""
from __future__ import annotations

import os
import random
import sys
import time
import types

# --- 最小化 psycopg2 桩：app.changes 仅在 insert_revision_changes 用到 Json ---
_psycopg2 = types.ModuleType("psycopg2")
_extras = types.ModuleType("psycopg2.extras")


class _Json:
    def __init__(self, value):
        self.value = value


_extras.Json = _Json
_psycopg2.extras = _extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.extras", _extras)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.changes import _candidate_pairs, relation_diffs  # noqa: E402
from app.derivation import EARLIER, SAME, Assertion, Derivation  # noqa: E402


# ------------------------------------------------------------------ 参考实现（旧全量口径）
def reference_diffs(strata, before_assertions, after_assertions):
    """旧实现：对全部已登记地层逐对比较提交前后关系结论与最优支撑路径。"""
    before = Derivation(before_assertions)
    after = Derivation(after_assertions)
    ordered = sorted(strata)
    out = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            old_rel, old_path = before.relation(a, b)
            new_rel, new_path = after.relation(a, b)
            old_path = old_path or []
            new_path = new_path or []
            if old_rel != new_rel or old_path != new_path:
                out.append(
                    {
                        "stratum_a": a,
                        "stratum_b": b,
                        "old_relation": old_rel,
                        "new_relation": new_rel,
                        "old_path": old_path,
                        "new_path": new_path,
                    }
                )
    return out


# ------------------------------------------------------------------ 随机有效状态生成
class IdGen:
    """随机证据编号（打乱字典序，压测路径并列时的编号序列比较）。"""

    def __init__(self, rng):
        self.rng = rng
        self.used = set()

    def new(self):
        while True:
            eid = f"E{self.rng.randrange(0x10000):04x}"
            if eid not in self.used:
                self.used.add(eid)
                return eid


def gen_state(rng, idgen, n_strata):
    """生成一组必然通过核验的断言：随机等价类划分 + 类内同一期边 + 类间 DAG 早于边。"""
    strata = [f"S{i:03d}" for i in range(n_strata)]
    groups = []
    for s in strata:
        if groups and rng.random() < 0.55:
            rng.choice(groups).append(s)
        else:
            groups.append([s])
    assertions = []
    for g in groups:
        rng.shuffle(g)
        for i in range(1, len(g)):  # 类内随机生成树，保证等价类连通
            j = rng.randrange(i)
            assertions.append(Assertion(idgen.new(), g[j], g[i], SAME))
        for _ in range(rng.randrange(0, 3)):  # 类内冗余同一期边（改道场景）
            if len(g) > 1:
                a, b = rng.sample(g, 2)
                assertions.append(Assertion(idgen.new(), a, b, SAME))
    rank = list(range(len(groups)))
    rng.shuffle(rank)  # 组间拓扑秩：早于边只从低秩组指向高秩组，商图必为 DAG
    for _ in range(rng.randrange(0, max(2, n_strata))):
        if len(groups) < 2:
            break
        i, j = rng.sample(range(len(groups)), 2)
        if rank[i] > rank[j]:
            i, j = j, i
        a, b = rng.choice(groups[i]), rng.choice(groups[j])
        assertions.append(Assertion(idgen.new(), a, b, EARLIER))
    return strata, assertions


def gen_after(rng, idgen, strata, before):
    """在 before 上施加一个随机批次（撤回若干 + 新增若干），保证最终状态通过核验。"""
    for _attempt in range(60):
        k = rng.randrange(0, min(4, len(before)) + 1)
        retracted = set(rng.sample(range(len(before)), k)) if before else set()
        after = [a for i, a in enumerate(before) if i not in retracted]
        for _ in range(rng.randrange(0, 4)):
            a, b = rng.sample(strata, 2)
            rel = rng.choice([SAME, EARLIER])
            after.append(Assertion(idgen.new(), a, b, rel))
        if not Derivation(after).validate():
            return after
    return None  # 多次尝试仍冲突则放弃本种子


# ------------------------------------------------------------------ 检查
def check_equal(tag, strata, before, after):
    got = relation_diffs(strata, before, after)
    want = reference_diffs(strata, before, after)
    assert got == want, (
        f"[{tag}] 增量与全量口径不一致：\n增量={got}\n全量={want}\n"
        f"before={before}\nafter={after}"
    )
    return got


def scenario_same_period_merge():
    """同一期合并：两个等价类被一条新同一期边并成一类，扩散对全覆盖。"""
    strata = ["A1", "A2", "B1", "B2", "C1"]
    before = [
        Assertion("E1", "A1", "A2", SAME),
        Assertion("E2", "B1", "B2", SAME),
        Assertion("E3", "A2", "B1", EARLIER),
    ]
    after = before + [Assertion("E4", "A2", "B2", SAME)]  # A 组与 B 组合并
    diffs = check_equal("same_period_merge", strata, before, after)
    pairs = {(d["stratum_a"], d["stratum_b"]) for d in diffs}
    assert ("A1", "B2") in pairs and ("A2", "B1") in pairs  # 原“早于”被合并为同一期


def scenario_same_period_split():
    """同一期拆分：撤回桥接边，等价类一分为二。"""
    strata = ["A1", "A2", "B1", "B2"]
    before = [
        Assertion("E1", "A1", "A2", SAME),
        Assertion("E2", "A2", "B1", SAME),
        Assertion("E3", "B1", "B2", SAME),
    ]
    after = [a for a in before if a.evidence_id != "E2"]
    diffs = check_equal("same_period_split", strata, before, after)
    pairs = {(d["stratum_a"], d["stratum_b"]) for d in diffs}
    assert ("A1", "B1") in pairs and ("A2", "B2") in pairs and ("A1", "A2") not in pairs


def scenario_reroute():
    """路径改道：撤回一条同一期边但连通性保持，最优路径变长。"""
    strata = ["A1", "A2", "A3"]
    before = [
        Assertion("E1", "A1", "A2", SAME),
        Assertion("E2", "A2", "A3", SAME),
        Assertion("E3", "A1", "A3", SAME),
    ]
    after = [a for a in before if a.evidence_id != "E3"]
    diffs = check_equal("reroute", strata, before, after)
    assert len(diffs) == 1 and diffs[0]["new_path"] == ["E1", "E2"]


def scenario_shortcut():
    """早于捷径：长链上加直达边，上游锥 × 下游锥全部对路径变短。"""
    strata = ["S1", "S2", "S3", "S4"]
    before = [
        Assertion("E1", "S1", "S2", EARLIER),
        Assertion("E2", "S2", "S3", EARLIER),
        Assertion("E3", "S3", "S4", EARLIER),
    ]
    after = before + [Assertion("E4", "S1", "S4", EARLIER)]
    diffs = check_equal("shortcut", strata, before, after)
    by_pair = {(d["stratum_a"], d["stratum_b"]): d for d in diffs}
    assert by_pair[("S1", "S4")]["new_path"] == ["E4"]
    assert by_pair[("S1", "S4")]["old_path"] == ["E1", "E2", "E3"]
    assert len(diffs) == 1  # 其余对最优路径不变


def scenario_parallel_retract():
    """平行断言撤回：撤字典序较大者零变化；撤较小者路径改走另一条。"""
    strata = ["S1", "S2"]
    before = [
        Assertion("E1", "S1", "S2", EARLIER),
        Assertion("E2", "S1", "S2", EARLIER),
    ]
    assert check_equal("parallel_retract_keep_best", strata, before, before[:1]) == []
    diffs = check_equal("parallel_retract_best", strata, before, before[1:])
    assert len(diffs) == 1 and diffs[0]["new_path"] == ["E2"]


def scenario_connect_components():
    """跨分量接通：一条早于边把两个原本无关的分量连成先后关系。"""
    strata = ["A1", "A2", "B1", "B2"]
    before = [
        Assertion("E1", "A1", "A2", SAME),
        Assertion("E2", "B1", "B2", SAME),
    ]
    after = before + [Assertion("E3", "A2", "B1", EARLIER)]
    diffs = check_equal("connect_components", strata, before, after)
    pairs = {(d["stratum_a"], d["stratum_b"]) for d in diffs}
    assert pairs == {("A1", "B1"), ("A1", "B2"), ("A2", "B1"), ("A2", "B2")}


def scenario_later_direction():
    """晚于方向：反向查询对的改道同样被覆盖。"""
    strata = ["S1", "S2", "S3"]
    before = [
        Assertion("E1", "S3", "S2", EARLIER),
        Assertion("E2", "S2", "S1", EARLIER),
        Assertion("E3", "S3", "S1", EARLIER),
    ]
    after = [a for a in before if a.evidence_id != "E3"]
    diffs = check_equal("later_direction", strata, before, after)
    assert len(diffs) == 1
    assert diffs[0]["old_relation"] == "later_than" or diffs[0]["new_relation"] == "later_than"


def scenario_no_net_change():
    """无净变更：前后断言集相同，候选为 None、结果为空。"""
    strata = ["S1", "S2"]
    before = [Assertion("E1", "S1", "S2", SAME)]
    assert relation_diffs(strata, before, list(before)) == []
    d = Derivation(before)
    assert _candidate_pairs(d, Derivation(list(before))) is None


def scenario_isolated_strata():
    """孤立地层与无关地层对：永不进入候选，结果与全量一致（空）。"""
    strata = [f"I{i}" for i in range(30)] + ["A", "B"]
    before = [Assertion("E1", "A", "B", SAME)]
    after = before + [Assertion("E2", "A", "B", EARLIER)]  # 组内早于冲突？不——A~B 同组
    # A~B 已同一期，再加 A<B 属组内早于，核验必拒；改用孤立层间新边：
    after = before + [Assertion("E2", "I0", "I1", SAME)]
    diffs = check_equal("isolated_strata", strata, before, after)
    assert len(diffs) == 1 and diffs[0]["stratum_a"] == "I0" and diffs[0]["stratum_b"] == "I1"


def scenario_random(rounds=300, seed0=20261003):
    rng = random.Random(seed0)
    idgen = IdGen(rng)
    done = 0
    for r in range(rounds):
        n = rng.randrange(2, 14)
        strata, before = gen_state(rng, idgen, n)
        after = gen_after(rng, idgen, strata, before)
        if after is None:
            continue
        check_equal(f"random#{r}", strata, before, after)
        done += 1
    print(f"随机回归：{done} 组前后状态全部与全量口径一致")


def scenario_performance():
    """性能冒烟：300 个地层分 5 个互不相干的区域，局部变更只触达本区域。"""
    rng = random.Random(7)
    idgen = IdGen(rng)
    strata, before = [], []
    for region in range(5):
        # 每区域 10 个同一期等价组（组内 6 层），组间一条早于链（有效 DAG）
        groups = []
        for g in range(10):
            members = [f"R{region}G{g:02d}S{i}" for i in range(6)]
            groups.append(members)
            strata.extend(members)
            for i in range(1, 6):
                before.append(Assertion(idgen.new(), members[i - 1], members[i], SAME))
        for g in range(1, 10):
            before.append(Assertion(idgen.new(), groups[g - 1][0], groups[g][0], EARLIER))
    # 局部变更：区域 2 内新增一条跨组早于捷径（组 1 -> 组 5）
    extra = Assertion("ZZZZ", "R2G01S0", "R2G05S0", EARLIER)
    after = before + [extra]

    t0 = time.perf_counter()
    got = relation_diffs(strata, before, after)
    t1 = time.perf_counter()
    want = reference_diffs(strata, before, after)
    t2 = time.perf_counter()
    assert got == want, "性能场景下增量与全量口径不一致"

    candidates = _candidate_pairs(Derivation(before), Derivation(after))
    total_pairs = len(strata) * (len(strata) - 1) // 2
    print(
        f"性能冒烟：地层 {len(strata)}，全库对数 {total_pairs}，"
        f"候选对 {len(candidates)}（{len(candidates) / total_pairs:.1%}），"
        f"实际变化 {len(got)}；增量 {t1 - t0:.3f}s vs 全量 {t2 - t1:.3f}s"
    )
    assert len(candidates) <= 60 * 60, "局部变更的候选对不应超出本区域规模"
    assert len(got) > 0
    assert all(d["stratum_a"].startswith("R2") and d["stratum_b"].startswith("R2") for d in got)


def main():
    scenario_same_period_merge()
    scenario_same_period_split()
    scenario_reroute()
    scenario_shortcut()
    scenario_parallel_retract()
    scenario_connect_components()
    scenario_later_direction()
    scenario_no_net_change()
    scenario_isolated_strata()
    print("定向场景：同一期合并 / 拆分 / 改道 / 捷径 / 平行撤回 / 跨分量 / 晚于 / 无净变更 / 孤立地层 全部通过")
    scenario_random()
    scenario_performance()
    print("全部通过：增量变化追踪与旧全量口径完全一致。")


if __name__ == "__main__":
    main()
