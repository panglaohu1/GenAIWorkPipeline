"""通用 WFC（波函数坍缩）求解器：槽位图 + 成对相容约束 + 权重。纯 Python，确定性。

- 槽位：任意可哈希键；每个槽位有初始可选瓦片（一元约束已在此过滤）。
- 关系：(a, b, rel)；rel 对应一张相容表 allowed[rel] = {(ta, tb), ...}，方向为 a→b。
- 选择：最小香农熵优先；传播：弧相容（AC-3）。
- 矛盾：换种子重启（限次），仍失败则对矛盾槽位回退到 fallback 瓦片（Townscaper 式安静降级）。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict, deque


class WFC:
    def __init__(self, domains: dict, weights: dict[str, float], allowed: dict[str, set[tuple[str, str]]],
                 relations: list[tuple], fallback: str):
        self.init = {k: list(v) for k, v in domains.items()}
        self.weights = weights
        self.allowed = allowed
        self.fallback = fallback
        self.adj = defaultdict(list)  # slot -> [(other, rel, forward?)]
        for a, b, rel in relations:
            if a in self.init and b in self.init:
                self.adj[a].append((b, rel, True))
                self.adj[b].append((a, rel, False))
        # 预计算：给定 a 的瓦片，b 可以是什么（正向 / 反向）
        self.fwd = {rel: defaultdict(set) for rel in allowed}
        self.bwd = {rel: defaultdict(set) for rel in allowed}
        for rel, pairs in allowed.items():
            for ta, tb in pairs:
                self.fwd[rel][ta].add(tb)
                self.bwd[rel][tb].add(ta)
        self.stats = {"restarts": 0, "fallbacks": 0}

    def _support(self, dom_a: set, rel: str, forward: bool) -> set:
        table = self.fwd[rel] if forward else self.bwd[rel]
        out = set()
        for t in dom_a:
            out |= table.get(t, set())
        return out

    def _propagate(self, dom: dict, start) -> bool:
        q = deque(start)
        while q:
            a = q.popleft()
            for b, rel, forward in self.adj[a]:
                ok = self._support(dom[a], rel, forward)
                nb = dom[b] & ok
                if nb != dom[b]:
                    if not nb:
                        return False
                    dom[b] = nb
                    q.append(b)
        return True

    def _entropy(self, d: set) -> float:
        ws = [self.weights.get(t, 1.0) for t in d]
        s = sum(ws)
        return math.log(s) - sum(w * math.log(w) for w in ws) / s

    def solve(self, seed: int = 0, max_restarts: int = 12) -> dict:
        keys = sorted(self.init, key=repr)
        for attempt in range(max_restarts + 1):
            rng = random.Random(seed * 1009 + attempt)
            dom = {k: set(self.init[k]) for k in keys}
            if not self._propagate(dom, [k for k in keys]):
                self.stats["restarts"] += 1
                continue
            failed = False
            while True:
                open_ = [k for k in keys if len(dom[k]) > 1]
                if not open_:
                    break
                k = min(open_, key=lambda x: (self._entropy(dom[x]) + rng.random() * 1e-3))
                opts = sorted(dom[k])
                ws = [self.weights.get(t, 1.0) for t in opts]
                pick = rng.choices(opts, weights=ws)[0]
                dom[k] = {pick}
                if not self._propagate(dom, [k]):
                    failed = True
                    break
            if not failed:
                return {k: next(iter(dom[k])) for k in keys}
            self.stats["restarts"] += 1
        # 安静降级：逐槽位贪心，冲突处用 fallback
        rng = random.Random(seed)
        out = {}
        for k in keys:
            opts = sorted(self.init[k])
            good = [t for t in opts if all(self._pair_ok(k, t, b, rel, fw, out) for b, rel, fw in self.adj[k])]
            if good:
                out[k] = rng.choices(good, weights=[self.weights.get(t, 1.0) for t in good])[0]
            else:
                out[k] = self.fallback if self.fallback in opts else opts[0]
                self.stats["fallbacks"] += 1
        return out

    def _pair_ok(self, a, ta, b, rel, forward, assigned) -> bool:
        if b not in assigned:
            return True
        tb = assigned[b]
        return (ta, tb) in self.allowed[rel] if forward else (tb, ta) in self.allowed[rel]
