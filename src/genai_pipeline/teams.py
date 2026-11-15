"""智能体的两种组织形态，以及从 AgentsGroup2026 引入的团队管理能力（本地精简实现）。

形态
  OPC（one-person company，一人公司）：一个 Agent 兼任导演、建模、评审（Oskar Agent）。
  team（多智能体协同）：组长 + 角色成员（Greg team，定义见 agents/greg_team.json）。

管理能力（概念来自 AgentsGroup2026；这里只用本机 LLM 重写最小可用的部分，不搬它的代码）
  1. 议事（Plaza，ORID + 五指共识）：①每位成员从自己的领域讲事实、风险、提案与验收；②组长归纳成结构化执行计划
     （每个阶段写清负责人、评审人、任务与验收）；③每位成员五指投票（1 = 根本反对 … 5 = 全力支持），
     有人投 1 指时组长按反对意见修订一次。计划写进任务快照，后续阶段的提示词按“本阶段负责人 + 评审 + 验收”注入。
  2. 人工确认：team 任务默认在议事后停下等你确认计划（审批关口 team_plaza）。
  3. token 记账：所有 LLM 调用按“当前发言成员 / 当前阶段负责人”归账（contextvar，调用方不用改）。
  4. 复盘与效能：每位成员按自己的领域给成果打分并提一条下次要带上的规则；效能 = 质量 × 成功率 ÷ 千 token。
  5. 棘轮（Ratchet）：效能只进不退——超过该 Agent 历史最好才推进一代，复盘提出的规则只有推进时才进入
     “待审规则”（仍要人在 Agent 页确认后才生效，不自动改风格规则）。
"""
from __future__ import annotations

import contextvars
import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

STAGES_IN_PLAN = ("plan", "concept", "model", "assemble", "review")
VERIFY = ("已设计", "已实现", "自动检查通过", "实景已看", "未验收")   # 交付状态分级：已验与未验分开写
STAGE_CN = {"plan": "方案规划", "concept": "目标概念图", "model": "Blender 建模", "assemble": "Godot 组装",
            "review": "试玩验收"}
FIST = {5: "全力支持", 4: "支持，有小顾虑", 3: "可以接受", 2: "有保留，希望修改", 1: "根本反对"}

_ledger: contextvars.ContextVar[dict | None] = contextvars.ContextVar("team_ledger", default=None)
_who: contextvars.ContextVar[str] = contextvars.ContextVar("team_who", default="")
_stage: contextvars.ContextVar[str] = contextvars.ContextVar("team_stage", default="")
_query: contextvars.ContextVar[str] = contextvars.ContextVar("team_query", default="")
_subs: contextvars.ContextVar[list | None] = contextvars.ContextVar("team_subagents", default=None)


# ───────────────────────── 子智能体（每次分派的一段工作） ─────────────────────────
# 成员是常设角色；子智能体是某个成员为一项具体工作开出的一次执行（议事发言、负责某阶段、投票、复盘……），
# 有自己的任务、状态、起止时间与 token。控制台按“已开启 / 完成”列出，和 Codex 的子代理面板对应。

def _open_sub(member_id: str, task: str, stage: str) -> dict | None:
    subs = _subs.get()
    if subs is None:
        return None
    import uuid
    rec = {"id": uuid.uuid4().hex[:8], "member": member_id, "task": task[:120], "stage": stage, "status": "running",
           "started": time.time(), "finished": None, "tokens": 0, "calls": 0}
    subs.append(rec)
    del subs[:-200]
    return rec


def _close_sub(rec: dict | None, ok: bool, tokens0: int, calls0: int, ledger: dict | None, by: str = "") -> None:
    if rec is None:
        return
    rec["status"] = "done" if ok else "failed"
    rec["finished"] = time.time()
    if ledger is not None:
        row = (ledger.get("by_member") or {}).get(by) if by else None
        rec["tokens"] = (row["total"] if row else ledger["total"]) - tokens0
        rec["calls"] = (row["calls"] if row else ledger["calls"]) - calls0


# ───────────────────────── 形态与名册 ─────────────────────────

def kind(agent: dict | None) -> str:
    return (agent or {}).get("kind") or "opc"


def is_team(profile: dict | None) -> bool:
    return kind(profile) == "team" and bool((profile or {}).get("members"))


def roster(profile: dict | None) -> list[dict]:
    return list((profile or {}).get("members") or [])


def member(profile: dict | None, member_id: str) -> dict | None:
    return next((m for m in roster(profile) if m["id"] == member_id), None)


def lead(profile: dict | None) -> dict | None:
    ms = roster(profile)
    return member(profile, (profile or {}).get("lead", "")) or (ms[0] if ms else None)


def stage_owner(profile: dict | None, stage: str) -> tuple[dict | None, list[dict]]:
    """本阶段负责人与评审人：先看议事计划（已确认的分工），没有再看成员定义里的默认分工。"""
    if not is_team(profile):
        return None, []
    ph = next((p for p in ((profile or {}).get("team_plan") or {}).get("phases", []) if p.get("stage") == stage), None)
    if ph and member(profile, ph.get("owner", "")):
        reviewers = [member(profile, r) for r in ph.get("reviewers", [])]
        return member(profile, ph["owner"]), [r for r in reviewers if r]
    owner = next((m for m in roster(profile) if stage in m.get("lead_stages", [])), None) or lead(profile)
    reviewers = [m for m in roster(profile) if stage in m.get("review_stages", []) and m is not owner]
    return owner, reviewers


def stage_rules(profile: dict | None, stage: str | None) -> str:
    """注入到各阶段提示词：本阶段负责人的职责与要求、评审人、议事计划里本阶段的任务与验收。"""
    if not is_team(profile) or not stage:
        return ""
    owner, reviewers = stage_owner(profile, stage)
    if owner is None:
        return ""
    text = (f"\n\n团队分工（{profile.get('name', '团队')}，本阶段：{STAGE_CN.get(stage, stage)}）：\n"
            f"- 负责人：{owner['name']}（{owner.get('title', '')}）——{owner.get('focus', '')}\n"
            f"  工作要求：{owner.get('instructions', '')}\n")
    if reviewers:
        text += "- 评审：" + "；".join(f"{r['name']}（{r.get('focus', '')[:60]}）" for r in reviewers) + \
                "。输出要能让评审人按各自领域检查。\n"
    ph = next((p for p in ((profile or {}).get("team_plan") or {}).get("phases", []) if p.get("stage") == stage), None)
    if ph:
        if ph.get("tasks"):
            text += "- 议事计划里本阶段的任务：" + "；".join(map(str, ph["tasks"]))[:900] + "\n"
        if ph.get("acceptance"):
            text += "- 本阶段验收：" + "；".join(map(str, ph["acceptance"]))[:600] + "\n"
    plan = (profile or {}).get("team_plan") or {}
    for key, label in (("scale", "尺度"), ("landmarks", "地标与抵达"), ("palette", "色板")):
        if plan.get(key):
            val = plan[key] if isinstance(plan[key], str) else json.dumps(plan[key], ensure_ascii=False)
            text += f"- 计划约定的{label}：{val[:400]}\n"
    return text


def public_roster(agent: dict) -> list[dict]:
    """控制台展示用（不含长提示词）。"""
    return [{k: m.get(k) for k in ("id", "name", "title", "studies", "focus", "lead_stages", "review_stages")}
            for m in roster(agent)]


# ───────────────────────── token 记账 ─────────────────────────

def _blank_ledger() -> dict:
    return {"total": 0, "prompt": 0, "completion": 0, "calls": 0, "by_member": {}, "by_stage": {}}


@contextmanager
def attribution(ctx, stage: str):
    """阶段执行期间把 LLM 用量记到 ctx.state['tokens']：归到本阶段负责人（OPC 则归到 Agent 本身）。"""
    profile = ctx.options.get("style_profile") or {}
    ledger = ctx.state.setdefault("tokens", _blank_ledger())
    owner, _ = stage_owner(profile, stage)
    who = owner["id"] if owner else profile.get("agent_id", "agent")
    ledger["budget"] = int((profile.get("config") or {}).get("token_budget") or 0)
    warned = ledger.get("over_budget", False)
    t1, t2, t3 = _ledger.set(ledger), _who.set(who), _stage.set(stage)
    t4 = _query.set(f"{ctx.run.get('title', '')} {ctx.run.get('brief', '')}")
    t5 = _subs.set(ctx.state.setdefault("subagents", []))
    row = ledger["by_member"].get(who) or {"total": 0, "calls": 0}
    from .pipeline import STAGE_LABELS
    sub = _open_sub(who, ("负责：" if owner else "") + STAGE_LABELS.get(stage, stage), stage)
    ok = False
    try:
        yield ledger
        ok = True
    finally:
        _close_sub(sub, ok, row["total"], row["calls"], ledger, who)
        _subs.reset(t5)
        _ledger.reset(t1)
        _who.reset(t2)
        _stage.reset(t3)
        _query.reset(t4)
        if ledger.get("over_budget") and not warned:
            ctx.emit("warn", f"token 超出该 Agent 的单任务预算 {ledger['budget']}（已用 {ledger['total']}）；继续执行，"
                     "可在 Agent 配置里调整预算或换更省的做法")
        try:
            ctx.save()
        except OSError:
            pass


@contextmanager
def speaking(member_id: str, task: str = ""):
    """成员发言 / 干活：token 归到该成员；带 task 时开一个子智能体记录（议事发言、投票、复盘等）。"""
    t = _who.set(member_id)
    ledger = _ledger.get()
    t0, c0 = (ledger["total"], ledger["calls"]) if ledger else (0, 0)
    sub = _open_sub(member_id, task, _stage.get()) if task else None
    ok = False
    try:
        yield
        ok = True
    finally:
        _close_sub(sub, ok, t0, c0, ledger)
        _who.reset(t)


def record_usage(usage: dict | None) -> None:
    ledger = _ledger.get()
    if ledger is None or not usage:
        return
    p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    tot = int(usage.get("total_tokens") or (p + c))
    ledger["total"] += tot
    ledger["prompt"] += p
    ledger["completion"] += c
    ledger["calls"] += 1
    if ledger.get("budget") and ledger["total"] > ledger["budget"]:
        ledger["over_budget"] = True
    for key, name in (("by_member", _who.get() or "agent"), ("by_stage", _stage.get() or "other")):
        row = ledger[key].setdefault(name, {"total": 0, "calls": 0})
        row["total"] += tot
        row["calls"] += 1


# ───────────────────────── 议事（Plaza） ─────────────────────────

def _excerpt(paths: list[str], limit: int = 1800) -> str:
    out = ""
    for rel in paths:
        p = (config.ROOT / rel).resolve()
        if p.is_relative_to(config.ROOT) and p.is_file():
            out += f"\n【{p.name}】\n" + p.read_text(errors="ignore")[: max(0, limit - len(out))]
        if len(out) >= limit:
            break
    return out


def _persona(profile: dict, m: dict) -> str:
    team_rules = "\n".join(f"- {r}" for r in profile.get("rules", [])[:12])
    return (f"你是 {profile.get('name')} 的成员「{m['name']}」（{m.get('title', '')}）。研究对象：{m.get('studies', '')}。\n"
            f"你的领域：{m.get('focus', '')}\n{m.get('instructions', '')}\n团队共同规则：\n{team_rules}\n"
            f"只讲自己领域内的事，简洁具体，可以施工、可以验收；不知道的标“待验证”，不编造作者参数。"
            f"\n你学过的资料摘录（只作背景）：{_excerpt(m.get('knowledge', []))}")


def _brief(ctx) -> str:
    return f"任务标题：{ctx.run.get('title', '')}\n需求：{ctx.run.get('brief', '')}\n构建工具链：目标概念图（本机 Flux）→ " \
           f"Blender 蓝图建模（逐资产、多轮评审）→ Godot 组装成可玩游戏 → 试玩验收。阶段：" + \
           "、".join(f"{k}={v}" for k, v in STAGE_CN.items())


def _v_speech(obj) -> str | None:
    if not isinstance(obj, dict):
        return "需要 JSON 对象"
    for k in ("facts", "risks", "proposals", "acceptance"):
        if not isinstance(obj.get(k), list):
            return f"缺少列表字段 {k}"
    return None


def _v_plan(ids: set[str]):
    def check(obj) -> str | None:
        if not isinstance(obj, dict) or not isinstance(obj.get("phases"), list) or not obj["phases"]:
            return "需要 phases 列表"
        for ph in obj["phases"]:
            if ph.get("stage") not in STAGES_IN_PLAN:
                return f"phase.stage 必须是 {list(STAGES_IN_PLAN)} 之一"
            if ph.get("owner") not in ids:
                return f"phase.owner 必须是成员 id：{sorted(ids)}"
            if any(r not in ids for r in ph.get("reviewers", [])):
                return "reviewers 只能填成员 id"
        return None
    return check


def _v_vote(obj) -> str | None:
    if not isinstance(obj, dict) or obj.get("fingers") not in (1, 2, 3, 4, 5):
        return "fingers 必须是 1–5 的整数"
    return None


def consensus(votes: list[dict]) -> dict:
    """五指共识：没有 1 指，且至少 60% 的人 ≥ 3 指。"""
    if not votes:
        return {"reached": False, "level": "none", "mean": 0.0, "blocking": []}
    fingers = [int(v["fingers"]) for v in votes]
    blocking = [v["member"] for v in votes if int(v["fingers"]) == 1]
    mean = sum(fingers) / len(fingers)
    reached = not blocking and sum(f >= 3 for f in fingers) >= 0.6 * len(fingers)
    level = "blocked" if blocking else ("strong" if mean >= 4 else "weak" if mean >= 3 else "none")
    return {"reached": reached, "level": level, "mean": round(mean, 2), "blocking": blocking}


PLAN_SCHEMA = ('{"goal":"一句话目标","scale":"人的比例/台阶/门洞等尺度约定","landmarks":"主地标与抵达顺序",'
               '"palette":[{"role":"水","hex":"#75ADB2","area":"40%"}],'
               '"phases":[{"stage":"plan|concept|model|assemble|review","owner":"成员id","reviewers":["成员id"],'
               '"tasks":["具体任务"],"deliverable":"交付物","acceptance":["可检查的验收标准"]}],'
               '"risks":["风险与对策"],"open_questions":["待你确认的问题"]}')


async def plaza(ctx) -> None:
    """阶段 team_plaza：ORID 议事 → 结构化执行计划 → 五指共识 →（可选）人工确认。"""
    from .llm import LLM
    profile = ctx.options.get("style_profile") or {}
    if not is_team(profile):
        ctx.emit("info", "单人 Agent（OPC）不需要议事，跳过")
        return
    from .pipeline import route
    llm = LLM(route(ctx, "plan"))
    ms = roster(profile)
    ids = {m["id"] for m in ms}
    head = lead(profile)
    brief = _brief(ctx)
    room = ctx.state.setdefault("team", {})
    room["members"] = public_roster(profile)
    speeches = []
    ctx.emit("team", f"{head['name']} 开会：{ctx.run.get('title', '')}（{len(ms)} 人，ORID：事实与风险 → 计划 → 五指投票）",
             {"phase": "open", "member": head["id"]})
    # ① 事实与风险（每人从自己的领域）
    for m in ms:
        with speaking(m["id"], "议事发言：事实、风险与提案"):
            sp = await llm.json([
                {"role": "system", "content": _persona(profile, m)},
                {"role": "user", "content": brief + "\n\n议事第一轮（ORID 的事实 + 风险）：从你的领域说——"
                 "facts：本任务里与你领域相关的已知条件；risks：最可能出问题的地方；proposals：你建议怎么做（≤5 条，"
                 "能施工）；acceptance：你会用什么标准验收。只输出 JSON："
                 '{"facts":[],"risks":[],"proposals":[],"acceptance":[]}'}],
                validate=_v_speech, thinking_budget=1024, on_delta=ctx.delta_emitter())
        speeches.append({"member": m["id"], "name": m["name"], **{k: sp.get(k, [])[:6] for k in
                                                                   ("facts", "risks", "proposals", "acceptance")}})
        ctx.emit("team", f"{m['name']}：" + "；".join(map(str, sp.get("proposals", [])[:3]))[:300],
                 {"phase": "speak", "member": m["id"], "speech": speeches[-1]})
    room["speeches"] = speeches
    # ② 组长成稿
    digest = json.dumps(speeches, ensure_ascii=False)[:9000]
    roster_txt = "；".join(f"{m['id']}={m['name']}（{m.get('focus', '')[:50]}）" for m in ms)
    plan_msgs = [
        {"role": "system", "content": _persona(profile, head)},
        {"role": "user", "content": brief + f"\n\n成员：{roster_txt}\n各成员第一轮发言：{digest}\n\n"
         "你是组长。把讨论归纳成可执行计划：phases 覆盖 plan、concept、model、assemble、review 五个阶段，"
         "每个阶段一位 owner（成员 id）和若干 reviewers；任务和验收要具体到能检查。冲突（例如生态密度与通行净空）"
         "由你裁决并写进 risks。只输出 JSON：" + PLAN_SCHEMA}]
    with speaking(head["id"], "组长成稿：结构化执行计划"):
        plan = await llm.json(plan_msgs, validate=_v_plan(ids), thinking_budget=2048, on_delta=ctx.delta_emitter())
    ctx.emit("team", f"{head['name']} 成稿：" + str(plan.get("goal", ""))[:200], {"phase": "plan", "plan": plan})
    # ③ 五指投票（有人 1 指则修订一次）
    revisions = 0
    while True:
        votes = []
        for m in ms:
            with speaking(m["id"], "五指投票"):
                v = await llm.json([
                    {"role": "system", "content": _persona(profile, m)},
                    {"role": "user", "content": "执行计划：" + json.dumps(plan, ensure_ascii=False)[:7000] +
                     "\n\n五指投票：5=全力支持愿意带头，4=支持有小顾虑，3=可以接受，2=有保留希望修改，1=根本反对。"
                     "从你的领域给出 fingers 和一句 reason；低于 4 时在 change 里写你要的具体修改。只输出 JSON："
                     '{"fingers":4,"reason":"","change":""}'}],
                    validate=_v_vote, thinking_budget=512)
            votes.append({"member": m["id"], "name": m["name"], "fingers": int(v["fingers"]),
                          "label": FIST[int(v["fingers"])], "reason": str(v.get("reason", ""))[:200],
                          "change": str(v.get("change", ""))[:300]})
        cs = consensus(votes)
        ctx.emit("team", f"五指投票：平均 {cs['mean']}，" + ("达成共识" if cs["reached"] else f"未达成（{cs['level']}）"),
                 {"phase": "vote", "votes": votes, "consensus": cs})
        if cs["reached"] or revisions >= 1:
            break
        revisions += 1
        objections = [v for v in votes if v["fingers"] <= 2]
        with speaking(head["id"], "按反对意见修订计划"):
            plan = await llm.json(plan_msgs + [
                {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                {"role": "user", "content": "以下成员有保留或反对，请逐条回应并修订计划（不能删掉验收）："
                 + json.dumps(objections, ensure_ascii=False) + "\n只输出修订后的完整 JSON。"}],
                validate=_v_plan(ids), thinking_budget=2048)
        ctx.emit("team", f"{head['name']} 按反对意见修订了计划", {"phase": "revise", "plan": plan})
    room.update({"plan": plan, "votes": votes, "consensus": cs, "revisions": revisions, "at": time.time()})
    _adopt_plan(ctx, plan)
    ctx.save()
    payload = await ctx.wait_approval("team_plaza")
    note = ((payload or {}).get("selections") or {}).get("note") if isinstance(payload, dict) else None
    if note:
        room["human_note"] = str(note)[:2000]
        plan.setdefault("open_questions", []).append("人工确认附言：" + room["human_note"])
        _adopt_plan(ctx, plan)


def _adopt_plan(ctx, plan: dict) -> None:
    """计划写进任务快照：后续阶段 rules_text 按它注入分工，重跑也沿用。"""
    profile = dict(ctx.options.get("style_profile") or {})
    profile["team_plan"] = plan
    ctx.options["style_profile"] = profile
    ctx.store.update_run(ctx.id, options=ctx.options)
    ctx.run["options"] = ctx.options


# ───────────────────────── 复盘、效能与棘轮 ─────────────────────────

def _agent_dir(agent_id: str) -> Path:
    from . import style_agents
    return style_agents._root() / agent_id


def _ratchet_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "ratchet.json"


def ratchet(agent_id: str) -> dict:
    p = _ratchet_path(agent_id)
    return json.loads(p.read_text()) if p.exists() else {"metrics": {}}


def advance(agent_id: str, key: str, value: float, evidence: dict) -> dict:
    """只进不退：首次记录 → 第 1 代；超过当前最好 → 推进一代；否则拒绝并说明。"""
    data = ratchet(agent_id)
    cur = data["metrics"].get(key)
    now = time.time()
    if cur is None or value > float(cur["value"]):
        gen = 1 if cur is None else int(cur["generation"]) + 1
        rec = {"generation": gen, "value": round(float(value), 6), "at": now, "evidence": evidence}
        hist = (cur or {}).get("history", []) + [dict(rec)]
        data["metrics"][key] = {**rec, "history": hist[-50:]}
        p = _ratchet_path(agent_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(p)
        return {"advanced": True, "generation": gen, "value": rec["value"],
                "reason": "首次记录" if cur is None else f"{cur['value']:.4f} → {value:.4f}"}
    return {"advanced": False, "generation": int(cur["generation"]), "value": float(cur["value"]),
            "reason": f"本次 {value:.4f} 未超过当前最好 {float(cur['value']):.4f}（只进不退）"}


def settle(ctx, member_scores: list[dict] | None = None) -> dict:
    """结算：质量（自动检查通过率，团队再与成员评分平均）× 成功率 ÷ 千 token → 效能，并推进棘轮。"""
    from .quality import assess
    profile = ctx.options.get("style_profile") or {}
    agent_id = profile.get("agent_id") or ctx.options.get("style_agent", "oskar")
    q = assess({**ctx.run, "options": ctx.options}, ctx.state)
    checks = q.get("checks", [])
    passed = sum(1 for c in checks if c.get("passed") is True)
    check_rate = passed / len(checks) if checks else 0.0
    hard_fail = any(c.get("passed") is False for c in checks)
    success = 0.5 if hard_fail else 1.0
    quality = check_rate
    if member_scores:
        mean = sum(float(s.get("score", 0)) for s in member_scores) / len(member_scores) / 10.0
        quality = round(0.5 * check_rate + 0.5 * mean, 4)
    tokens = int((ctx.state.get("tokens") or {}).get("total") or 0)
    eff = quality * success / max(tokens / 1000.0, 1.0)
    result = {"agent": agent_id, "kind": kind(profile), "quality": round(quality, 4), "check_rate": round(check_rate, 4),
              "success": success, "tokens": tokens, "efficiency": round(eff, 6), "at": time.time()}
    result["ratchet"] = advance(agent_id, "efficiency", eff, {"run": ctx.id, "quality": result["quality"],
                                                                "tokens": tokens})
    from . import agent_config   # 技能回写成败：生命周期按 Filter→Improve→Verify→Solidify 调整
    result["skills"] = agent_config.settle_skills(agent_id, ctx.id, (ctx.state.get("tokens") or {}).get("skills_used"),
                                                  success == 1.0 and quality >= 0.6)
    ctx.state["settlement"] = result
    ctx.save()
    return result


async def retro(ctx) -> None:
    """阶段 team_retro：每位成员按自己的领域复盘打分，并提一条下次要带上的规则；结算效能与棘轮。"""
    from .llm import LLM, user_message
    from .pipeline import route
    profile = ctx.options.get("style_profile") or {}
    if not is_team(profile):
        res = settle(ctx)
        ctx.emit("result", f"结算：效能 {res['efficiency']:.4f}（{res['ratchet']['reason']}）", res)
        return
    from .quality import assess
    q = assess({**ctx.run, "options": ctx.options}, ctx.state)
    checks = [{k: c.get(k) for k in ("name", "passed", "detail")} for c in q.get("checks", [])][:30]
    arts = [{"label": a.get("label") or a.get("kind"), "kind": a.get("kind"), "stage": a.get("stage")}
            for a in ctx.store.artifacts(ctx.id)][-40:]
    evidence_names = {str(c["name"]) for c in checks} | {str(a["label"]) for a in arts}
    facts = {"checks": checks, "artifacts": arts, "plan": (profile.get("team_plan") or {}).get("phases", []),
             "tokens": {k: (ctx.state.get("tokens") or {}).get(k) for k in ("total", "by_member", "by_stage")}}
    shots = [s for s in (ctx.state.get("review", {}).get("screenshots") or ctx.state.get("final_shots") or [])
             if isinstance(s, str) and Path(s).is_file()][:2]
    llm = LLM(route(ctx, "observe" if shots else "plan"))
    scores = []
    for m in roster(profile):
        with speaking(m["id"], "复盘：学习落实、交付状态与下次规则"):
            r = await llm.json([
                {"role": "system", "content": _persona(profile, m)},
                user_message("复盘（只写你的领域，参照 Greg team 的 RESULTS.md 写法）。事实：" + json.dumps(facts, ensure_ascii=False)[:7000] +
                             "\n给出：score（0–10）；learned（本次学习并落实了什么，≤3 条）；deliverables（你交付的东西，每条写"
                             f" item、status（只能是 {'/'.join(VERIFY)}）、evidence（引用上面的检查名或产物名；没有就留空）；"
                             "issues（具体位置与原因）；next_rule（下次要带上的一条规则，一句话，能检查）。"
                             "不要把没验证的写成已验证，CPU / 自动检查通过不等于实景已看。只输出 JSON："
                             '{"score":6,"learned":[],"deliverables":[{"item":"","status":"已实现","evidence":""}],"issues":[],"next_rule":""}', shots)],
                validate=lambda o: None if isinstance(o, dict) and isinstance(o.get("score"), (int, float)) else "需要 score",
                thinking_budget=1024)
        dels = []
        for d in (r.get("deliverables") or [])[:6]:
            if not isinstance(d, dict) or not d.get("item"):
                continue
            st, ev = str(d.get("status", "")), str(d.get("evidence", ""))[:200]
            if st not in VERIFY:
                st = "未验收"
            if st in ("自动检查通过", "实景已看") and not any(n and n in ev for n in evidence_names):
                st = "未验收（证据不符）"   # 不虚报：声称已验证却引用不到真实检查 / 产物
            dels.append({"item": str(d["item"])[:200], "status": st, "evidence": ev})
        scores.append({"member": m["id"], "name": m["name"], "score": max(0.0, min(10.0, float(r["score"]))),
                       "learned": [str(x)[:200] for x in (r.get("learned") or [])][:3], "deliverables": dels,
                       "went_well": r.get("went_well", [])[:4], "issues": r.get("issues", [])[:5],
                       "next_rule": str(r.get("next_rule", ""))[:300]})
        ctx.emit("team", f"{m['name']} 复盘 {scores[-1]['score']:.1f} 分：{scores[-1]['next_rule'][:120]}",
                 {"phase": "retro", "member": m["id"], "retro": scores[-1]})
    head = lead(profile)
    with speaking(head["id"], "组长总评：未完成项与下一轮安排"):
        nxt = await llm.json([
            {"role": "system", "content": _persona(profile, head)},
            {"role": "user", "content": "成员复盘：" + json.dumps(scores, ensure_ascii=False)[:8000] + "\n检查：" +
             json.dumps(checks, ensure_ascii=False)[:3000] + "\n你是组长，写总评：summary（两三句，已验与未验分开说）；"
             "unfinished（尚未完成及下一轮安排，按 P0/P1 排，每条 priority、item、hours（估算如 0.5–1）、exit（退出条件））。"
             "估算不是已耗时；不把未完成算进成果。只输出 JSON："
             '{"summary":"","unfinished":[{"priority":"P0","item":"","hours":"0.5–1","exit":""}]}'}],
            validate=lambda o: None if isinstance(o, dict) and isinstance(o.get("unfinished"), list) else "需要 unfinished 列表",
            thinking_budget=1024)
    review = {"summary": str(nxt.get("summary", ""))[:1200],
              "unfinished": [{k: str(u.get(k, ""))[:300] for k in ("priority", "item", "hours", "exit")}
                             for u in nxt.get("unfinished", []) if isinstance(u, dict)][:10]}
    res = settle(ctx, scores)
    room = ctx.state.setdefault("team", {})
    room["retro"] = scores
    room["review"] = review
    _write_results(ctx, profile, scores, review, checks, res)
    from . import agent_config   # 技能萃取：复盘规则 → 草稿技能（审核发布后才参与路由）
    room["skills_drafted"] = [s["id"] for s in agent_config.extract_from_retro(
        res["agent"], ctx.id, [s["next_rule"] for s in scores if s["next_rule"]])]
    if res["ratchet"]["advanced"]:   # 推进时才把复盘规则放进待审清单（人确认后才写进风格规则）
        pending = [s["next_rule"] for s in scores if s["next_rule"]]
        _save_pending(res["agent"], ctx.id, pending)
        room["pending_rules"] = pending
    ctx.emit("result", f"团队结算：质量 {res['quality']:.2f} · token {res['tokens']} · 效能 {res['efficiency']:.4f}"
             f"（{res['ratchet']['reason']}）", res)


def _write_results(ctx, profile: dict, scores: list[dict], review: dict, checks: list[dict], res: dict) -> None:
    """按 Greg team 的 RESULTS.md 写法落一份任务成果文档：每个人做了什么、验证结果、尚未完成及下一轮安排。"""
    plan = profile.get("team_plan") or {}
    subs = ctx.state.get("subagents") or []
    L = [f"# {ctx.run.get('title', '')} · 团队成果（{profile.get('name', '')}）", "",
         f"任务 {ctx.id}。成员研究具名创作者的公开方法，不是作者本人或背书。交付状态分级：{' / '.join(VERIFY)}；"
         "自动检查通过不等于实景已看，估算不是已耗时。", ""]
    if review.get("summary"):
        L += ["## 组长总评", "", review["summary"], ""]
    L += ["## 每个人做了什么", "", "| 成员 / 方法来源 | 已学习并落实 | 交付及状态 |", "|---|---|---|"]
    by = {m["id"]: m for m in roster(profile)}
    for s in scores:
        m = by.get(s["member"], {})
        mine = [x for x in subs if x["member"] == s["member"]]
        own = [STAGE_CN.get(p["stage"], p["stage"]) for p in plan.get("phases", []) if p.get("owner") == s["member"]]
        dl = "；".join(f"{d['item']}（{d['status']}{'，证据：' + d['evidence'] if d['evidence'] else ''}）" for d in s["deliverables"])
        meta = f"负责：{'、'.join(own) or '—'}；复盘 {s['score']:.1f} 分；{len(mine)} 个子智能体、{sum(x.get('tokens', 0) for x in mine)} token"
        cell = lambda t: str(t).replace("|", "／").replace("\n", " ")
        L.append(f"| {cell(s['name'])} / {cell(m.get('studies', ''))} | {cell('；'.join(s['learned']) or '—')} | "
                 f"{cell(dl or '—')}。{cell(meta)} |")
    L += ["", "## 验证结果", ""]
    for c in checks:
        mark = "通过" if c.get("passed") is True else "未通过" if c.get("passed") is False else "待验证"
        L.append(f"- {mark} · {c.get('name')}{('：' + str(c.get('detail'))) if c.get('detail') else ''}")
    L += ["", f"效能：质量 {res['quality']} × 成功率 {res['success']} ÷ 千 token（共 {res['tokens']}）= {res['efficiency']}；"
          f"棘轮：{res['ratchet']['reason']}。", ""]
    if review.get("unfinished"):
        L += ["## 尚未完成及下一轮安排", ""]
        for i, u in enumerate(review["unfinished"], 1):
            L.append(f"{i}. **{u['priority']}，{u['hours']} 小时：{u['item']}**" + (f" 退出条件：{u['exit']}" if u["exit"] else ""))
        L += ["", "以上为估算，不是已耗时或后台定时器。"]
    issues = [f"{s['name']}：{x}" for s in scores for x in s["issues"]]
    if issues:
        L += ["", "## 成员发现的问题", ""] + [f"- {x}" for x in issues]
    path = ctx.dir / "RESULTS.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    ctx.artifact("results", path, "团队成果 RESULTS.md")


def _save_pending(agent_id: str, run_id: str, rules: list[str]) -> None:
    """效能推进时，把成员复盘规则存成一份“学习版本”草稿（沿用 Agent 页的审核 → 应用流程，不自动生效）。"""
    from . import style_agents
    import uuid
    active = style_agents.snapshot(agent_id)
    base = next((p for p in style_agents.profiles(agent_id) if p["version"] == active["version"]), None)
    keep = [r for r in (base or {}).get("rules", [])]
    new = [{"rule": r, "sources": [f"run:{run_id}"]} for r in rules if r and all(r != k["rule"] for k in keep)]
    version = "retro-" + uuid.uuid4().hex[:8]
    profile = {"version": version, "agent_id": agent_id, "created": time.time(), "run_id": run_id,
               "summary": f"团队复盘（{run_id}）：效能超过历史最好，成员提出 {len(new)} 条新规则，待审核应用。",
               "rules": (keep + new)[:12], "source_ids": [f"run:{run_id}"], "sources": [],
               "based_on": active["version"], "origin": "team_retro"}
    style_agents._save(_agent_dir(agent_id) / "profiles" / f"{version}.json", profile)
    p = _agent_dir(agent_id) / "pending_rules.json"
    data = json.loads(p.read_text()) if p.exists() else []
    data.append({"run": run_id, "rules": rules, "version": version, "at": time.time()})
    p.write_text(json.dumps(data[-20:], ensure_ascii=False, indent=2))


def pending_rules(agent_id: str) -> list[dict]:
    p = _agent_dir(agent_id) / "pending_rules.json"
    return json.loads(p.read_text()) if p.exists() else []
