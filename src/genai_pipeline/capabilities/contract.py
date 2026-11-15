"""能力契约（与 TigerInBamboo 的能力库同一套约定，Python 侧）。

一个能力 = 三样东西（REACodeRefactoring/src/capabilities/README.md）：
  meta    id、所属层（tier：scene3d / object / gscene / voxel / planet / deliver …）、名字、做什么、来源、
          能作用的物件、Router 关键词、参数（含范围与默认值）、origin（native = 本管线；reacode / workshop = 外部能力库）
  match   (subject, params) → 能作用的物件 [{id, score, reason, …}]；reason 用一句中文说明为什么匹配
  run     (subject, targets, params) → 候选（在副本上执行，不改原件）
外加 verify：独立检查「执行前 / 执行后」，每项 pass / warn / fail；fail 的候选不能写入。

运行时（对应 TigerMessenger 的 tier-agent.js + scene-authoring/agent/runtime.js）：
  Router       改造方向关键词 × 扫描结果（只推荐真能作用的能力；已在管线里启用的直接入选）
  Watchdog     每次执行有总超时；超时取消并重试一次（外部能力走 Node 子进程，超时即杀进程重建）
  经验沉淀      每次预览 / 写入 / 放弃记一行到 data/experience/<层>.jsonl，按能力汇总
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

STATUS_ORDER = {"pass": 0, "warn": 1, "fail": 2}


def params_for(meta: dict, given: dict | None = None) -> dict:
    """按 meta.params 的范围夹紧参数；没给的取默认（与 registry.js 的 defaults / paramsFor 一致）。"""
    given = given or {}
    out = {}
    for k, d in (meta.get("params") or {}).items():
        v = given.get(k, d.get("default"))
        if d.get("type") == "bool":
            out[k] = bool(v)
        elif d.get("type") in ("list", "str"):
            out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            v = max(d.get("min", float("-inf")), min(d.get("max", float("inf")), v))
            out[k] = int(round(v)) if d.get("type") == "int" else v
        else:
            out[k] = d.get("default")
    for k, v in given.items():   # 不在 meta.params 里的透传（例如外部能力自己的参数）
        out.setdefault(k, v)
    return out


def summarize_checks(checks: list[dict]) -> dict:
    """[{id, name, status, detail}] → {status, checks, summary}（与 verify.js 的返回形状一致）。"""
    worst = max((STATUS_ORDER.get(c.get("status"), 2) for c in checks), default=0)
    status = {0: "pass", 1: "warn", 2: "fail"}[worst]
    fail = sum(1 for c in checks if c.get("status") == "fail")
    warn = sum(1 for c in checks if c.get("status") == "warn")
    summary = f"{fail} 项不通过" if fail else (f"通过，{warn} 项需留意" if warn else "全部通过")
    return {"status": status, "checks": checks, "summary": summary}


@dataclass
class Capability:
    meta: dict
    match: Callable[..., Any]
    run: Callable[..., Any] | None = None
    verify: Callable[..., Any] | None = None
    revert: Callable[..., Any] | None = None   # 验证 fail 时撤回（在原件上执行的能力需要）

    @property
    def id(self) -> str:
        return self.meta["id"]

    def describe(self) -> dict:
        meta = {k: v for k, v in self.meta.items() if not callable(v)}   # 函数（例如 skip_note）不进目录
        return {**meta, "params": meta.get("params") or {}, "executable": self.run is not None}


async def _maybe(v):
    return await v if inspect.isawaitable(v) else v


class Registry:
    """一层（tier）的能力登记表：扫描、路由、执行计划、验证。"""

    def __init__(self, tier: str, caps: list[Capability] | None = None):
        self.tier = tier
        self.caps: dict[str, Capability] = {}
        for c in caps or []:
            self.add(c)

    def add(self, cap: Capability) -> None:
        meta = cap.meta
        if not meta.get("id") or not meta.get("name") or not meta.get("keywords"):
            raise ValueError(f"能力 meta 不完整：{meta}")
        if meta["id"] in self.caps:
            raise ValueError(f"能力 id 重复：{meta['id']}")
        meta.setdefault("tier", self.tier)
        self.caps[meta["id"]] = cap

    def get(self, cid: str) -> Capability | None:
        return self.caps.get(cid)

    def describe(self) -> list[dict]:
        return [c.describe() for c in self.caps.values()]

    async def match_all(self, subject: Any, limit: int = 12) -> list[dict]:
        out = []
        for cap in self.caps.values():
            try:
                rows = await _maybe(cap.match(subject, params_for(cap.meta)))
            except Exception as e:  # noqa: BLE001 — 一个能力扫描出错不影响其它
                out.append({"id": cap.id, "error": f"{type(e).__name__}: {e}"[:300], "count": 0, "targets": []})
                continue
            rows = sorted(rows or [], key=lambda r: -float(r.get("score", 0)))
            out.append({"id": cap.id, "count": sum(int(r.get("count", 1)) for r in rows), "targets": rows[:limit]})
        return out


def route(goal: str, caps: list[dict], matches: list[dict], selected: list[str] | tuple = ()) -> list[dict]:
    """Router：改造方向（文字）关键词命中 + 场景里确实有可作用物件；已在管线里启用（selected）的直接入选。
    与 registry.js routeGoal / tier-agent.js routeByKeywords 同一规则（确定性，不调 LLM）。"""
    g = (goal or "").lower()
    out = []
    for meta in caps:
        m = next((x for x in matches if x.get("id") == meta["id"]), None)
        if not m or not m.get("count"):
            continue
        hits = [k for k in meta.get("keywords") or [] if k.lower() in g]
        pinned = meta["id"] in selected
        if not hits and not pinned:
            continue
        why = "；".join(t.get("reason", "") for t in (m.get("targets") or [])[:3] if t.get("reason"))
        out.append({"id": meta["id"], "tier": meta.get("tier"), "matches": m["count"],
                    "score": len(hits) * 2 + (5 if pinned else 0) + min(1.0, m["count"] / 10),
                    "why": (f"改造方向提到「{'、'.join(hits)}」；" if hits else "已在管线中启用；") + why})
    return sorted(out, key=lambda r: -r["score"])


class ExperienceStore:
    """经验沉淀：data/experience/<层>.jsonl，每次预览 / 写入 / 放弃一行；按能力汇总（与 runtime.js experienceStore 一致）。"""

    def __init__(self, path: Path, keep: int = 2000):
        self.path = Path(path)
        self.keep = keep

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def add(self, row: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.time(), **row}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        rows = self.read()
        if len(rows) > self.keep * 1.2:
            self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-self.keep:]), encoding="utf-8")
        return row

    def summary(self, cid: str) -> dict:
        rows = [r for r in self.read() if any(s.get("id") == cid for s in r.get("steps") or [])]
        runs = [r for r in rows if r.get("outcome") == "previewed"]
        fails = [r for r in runs if r.get("verify") == "fail"]
        return {"runs": len(runs), "passed": len(runs) - len(fails), "failed": len(fails),
                "committed": sum(1 for r in rows if r.get("outcome") == "committed"),
                "lastFailure": (fails[-1].get("why") if fails else "") or ""}


async def with_watchdog(fn: Callable[[], Awaitable[Any]], *, total: float, name: str,
                        emit: Callable[[dict], None] | None = None) -> Any:
    """Watchdog + withRetry：超过 total 秒取消，重试一次（调用方每次给新的子进程 / 新副本）。"""
    for attempt in (1, 2):
        try:
            return await asyncio.wait_for(fn(), timeout=total)
        except asyncio.TimeoutError:
            if emit:
                emit({"type": "recover", "name": name, "why": f"{name} 超过 {total:g} 秒", "attempt": attempt})
            if attempt == 2:
                raise TimeoutError(f"{name} 两次都超过 {total:g} 秒")


@dataclass
class Candidate:
    tier: str
    goal: str
    steps: list[dict]
    report: list[dict]
    verification: dict
    result: Any = None
    id: str = field(default_factory=lambda: f"cand-{int(time.time() * 1000):x}")


class TierAgent:
    """一层的 super agent：scan → route → generate（在副本上执行）→ verify → commit / discard，事件与经验都记下。
    层与层的差异只在 hooks：scan(subject)、run(subject, steps)、verify(subject, result, steps)、commit(result)。"""

    def __init__(self, tier: str, *, hooks: dict, caps: list[dict], experience: ExperienceStore | None = None,
                 emit: Callable[[dict], None] | None = None, timeouts: dict | None = None):
        self.tier, self.hooks, self.caps = tier, hooks, caps
        self.exp = experience
        self.emit = emit or (lambda e: None)
        self.T = {"scan": 60, "run": 90, "verify": 60, **(timeouts or {})}
        self.scan_result: list[dict] | None = None
        self.log: list[dict] = []

    def _ev(self, e: dict) -> None:
        e = {"tier": self.tier, "at": time.time(), **e}
        self.log.append(e)
        self.emit(e)

    async def scan(self, subject: Any) -> list[dict]:
        self._ev({"type": "phase", "phase": "scan"})
        self.scan_result = await with_watchdog(lambda: self.hooks["scan"](subject), total=self.T["scan"],
                                               name=f"{self.tier} · 扫描", emit=self._ev)
        self._ev({"type": "phase", "phase": "scanned", "count": sum(m.get("count", 0) for m in self.scan_result)})
        return self.scan_result

    def route(self, goal: str, selected: list[str] | tuple = ()) -> list[dict]:
        return route(goal, self.caps, self.scan_result or [], selected)

    async def generate(self, subject: Any, steps: list[dict], goal: str = "") -> Candidate:
        if not steps:
            raise ValueError("管线里没有启用的可执行能力")
        self._ev({"type": "phase", "phase": "transform", "steps": [s["id"] for s in steps]})
        res = await with_watchdog(lambda: self.hooks["run"](subject, steps), total=self.T["run"],
                                  name=f"{self.tier} · 改造", emit=self._ev)
        self._ev({"type": "phase", "phase": "verify"})
        ver = await with_watchdog(lambda: self.hooks["verify"](subject, res, steps), total=self.T["verify"],
                                  name=f"{self.tier} · 验证", emit=self._ev)
        cand = Candidate(tier=self.tier, goal=goal, steps=steps, report=res.get("report") or [], verification=ver, result=res)
        self._remember(cand, "previewed")
        self._ev({"type": "phase", "phase": "review", "status": ver.get("status")})
        return cand

    async def commit(self, cand: Candidate) -> Any:
        if cand.verification.get("status") == "fail":
            raise ValueError("验证未通过的候选不能写入")
        out = await _maybe(self.hooks["commit"](cand))
        self._remember(cand, "committed")
        self._ev({"type": "phase", "phase": "committed"})
        return out

    def discard(self, cand: Candidate, why: str = "") -> None:
        self._remember(cand, "discarded", why)
        self._ev({"type": "phase", "phase": "discarded", "why": why})

    def _remember(self, cand: Candidate, outcome: str, why: str = "") -> None:
        if not self.exp:
            return
        fails = "；".join(f"{c.get('name')}：{c.get('detail', '')}" for c in cand.verification.get("checks") or []
                         if c.get("status") == "fail")
        self.exp.add({"id": cand.id, "tier": self.tier, "goal": cand.goal[:200],
                      "steps": [{"id": r.get("id"), "status": r.get("status")} for r in cand.report] or
                               [{"id": s["id"]} for s in cand.steps],
                      "verify": cand.verification.get("status"), "why": why or fails, "outcome": outcome})
