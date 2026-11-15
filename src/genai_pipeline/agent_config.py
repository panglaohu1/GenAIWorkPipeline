"""Agent 配置与管理、技能库（Skill）——从 AgentsGroup2026 引入的核心能力，本地精简重写。

对应关系（AgentsGroup2026 → 本系统）
  AgentProfile（基本信息 / 人格 / 技能 / 工具 / 权限与通道，五步向导）→ 每个 Agent 的“配置”覆盖层
      data/agents/<id>/config.json；内置定义文件不改，配置只叠加。团队可以逐个成员配置（模型、技能、启用）。
  SkillDefinition（instructions、required_tools、lifecycle、usage/success/fail、effectiveness、adopted_by）→ 技能库
      内置技能 skills/library/*.json（随仓库发布）+ 用户 / 萃取技能 data/skills/*.json。
  SkillRouter（BM25 两段检索 + 生命周期加权 → 注入系统提示词）→ route_skills()：按“本阶段 + 任务简介”从该 Agent
      已绑定的技能里选前 K 条，写进各阶段提示词；用到的技能记进任务快照，任务结算后回写成败与效能。
  技能萃取（TSE）→ 团队复盘时成员提出的“下次要带上的规则”生成草稿技能（draft），人审核发布后才参与路由。
  生命周期 Filter→Improve→Verify→Solidify：draft →（发布）published →（≥3 次使用且成功率 ≥70%）verified
      →（≥8 次且 ≥80%）solidified；≥3 次且成功率 <40% 降为 degraded。
  autonomy_level / token_budget / model：自主级别（L1 只建议 … L4 全自主，决定默认审批关口）、单任务 token 预算
      （超出时在日志告警，不中断）、模型路由。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from . import config

_lock = threading.RLock()

LIFECYCLE = ("draft", "published", "verified", "solidified", "degraded")
LIFECYCLE_CN = {"draft": "草稿", "published": "已发布", "verified": "已验证", "solidified": "已固化", "degraded": "已降级"}
LIFECYCLE_MULT = {"solidified": 1.14, "verified": 1.12, "published": 1.08, "draft": 0.9, "degraded": 0.72}
STAGES = ("plan", "concept", "model", "assemble", "review", "oskar", "oskar_plan", "oskar_build", "atlas_brief", "atlas_targets",
          "atlas_scenes", "team_plaza", "team_retro")
# 流水线实际注入技能时用的阶段名（plan / concept / model / assemble / review / oskar …）与技能上细分的阶段名对应：
# Oskar 引擎构建场景时用 "oskar"，旧技能写的是 oskar_plan / oskar_build，二者都应命中。
STAGE_ALIASES = {"oskar": {"oskar_plan", "oskar_build", "atlas_scenes"}, "plan": {"atlas_brief", "oskar_plan"},
                 "concept": {"atlas_targets"}, "assemble": {"planet_godot", "oskar_godot", "atlas_godot"}}


def stage_matches(stage: str, stages) -> bool:
    stages = set(stages or [])
    return stage in stages or bool(stages & STAGE_ALIASES.get(stage, set()))
TOOLS = {   # 本系统可绑定的工具（能力），对应流水线里的真实服务
    "flux": {"name": "Flux 生图", "desc": "本机 FLUX.1-schnell 出目标图 / 概念图", "stages": ["concept", "atlas_targets"]},
    "blender": {"name": "Blender 建模", "desc": "MCP 驱动 Blender：蓝图建模、Oskar 引擎构建、渲染", "stages": ["model", "oskar_build"]},
    "shape3d": {"name": "Hunyuan3D 图生 3D", "desc": "本机 Hunyuan3D-2mini Turbo：按裁剪图生成核心资产形体，Blender 再整理着色（代替 Tripo）",
                "stages": ["model"]},
    "godot": {"name": "Godot 组装", "desc": "Godot 4 工程生成、试玩截图与硬检查", "stages": ["assemble", "oskar_godot"]},
    "sam": {"name": "SAM 分割", "desc": "目标图分割，给读图与评审提供客观数据", "stages": ["oskar_plan", "decompose"]},
    "depth": {"name": "深度定位", "desc": "本机 Depth Anything 3 度量版（MoGe-2 内参）：从目标图测出物件尺寸、摆放、相机与水面 / 地面",
              "stages": ["decompose"]},
    "vision_review": {"name": "视觉评审", "desc": "多模态模型看图打分、差异台账", "stages": ["model", "review"]},
}
MODELS = {"main": "Qwen3.8-27B oQ6（主力）"}
AUTONOMY = {1: "L1 只出建议：计划、目标图、交付前都停下等你", 2: "L2 计划确认：团队计划要你确认，之后自动",
            3: "L3 交付前确认：只在完整流程试玩时停下", 4: "L4 全自主：不停下（OPC 默认，和以前一样）"}
AUTONOMY_GATES = {1: ["team_plaza", "concept", "journey"], 2: ["team_plaza"], 3: ["journey"], 4: []}


def _data() -> Path:
    from . import style_agents
    return style_agents._root()


def _skills_dir() -> Path:
    return _data().parent / "skills"


def _builtin_dir() -> Path:
    return config.ROOT / "skills" / "library"


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ───────────────────────── Agent 配置 ─────────────────────────

def default_config(agent: dict) -> dict:
    cfg = {"personality": {"tone": "专业、直接", "response_style": "简洁", "creativity": 0.5,
                           "expertise_areas": list(agent.get("tags", []))},
           "model": {}, "skills": list(agent.get("default_skills", [])), "tools": list(TOOLS),
           "autonomy_level": 2 if agent.get("kind") == "team" else 4, "token_budget": 0, "image_backend": "", "members": {}}
    for m in agent.get("members", []):
        cfg["members"][m["id"]] = {"enabled": True, "model": "", "skills": list(m.get("default_skills", [])),
                                   "extra_instructions": ""}
    return cfg


def get_config(agent_id: str) -> dict:
    from . import style_agents
    agent = style_agents.get(agent_id)
    cfg = default_config(agent)
    p = _data() / agent_id / "config.json"
    if p.exists():
        saved = json.loads(p.read_text())
        for k, v in saved.items():
            if k == "members" and isinstance(v, dict):
                for mid, mv in v.items():
                    if mid in cfg["members"] and isinstance(mv, dict):
                        cfg["members"][mid].update(mv)
            elif k == "personality" and isinstance(v, dict):
                cfg["personality"].update(v)
            else:
                cfg[k] = v
    # 删掉的工具（如已删除模型的 qwen_image）自动解绑；新增的工具（深度定位）默认启用
    cfg["tools"] = [t for t in cfg.get("tools", []) if t in TOOLS] + [t for t in ("depth",) if t not in cfg.get("tools", [])]
    # 已删除的模型（Bonsai / Sage / gemma 等）从角色和成员的模型选择里清掉，回到系统默认路由（Qwen3.8）
    cfg["model"] = {r: m for r, m in (cfg.get("model") or {}).items() if m in MODELS}
    for row in cfg["members"].values():
        if row.get("model") and row["model"] not in MODELS:
            row["model"] = ""
    if cfg.get("image_backend") not in ("", "flux"):
        cfg["image_backend"] = ""
    known = {s["id"] for s in list_skills()}   # 删掉的技能自动解绑
    cfg["skills"] = [i for i in cfg.get("skills", []) if i in known]
    for row in cfg["members"].values():
        row["skills"] = [i for i in row.get("skills", []) if i in known]
    return cfg


def save_config(agent_id: str, data: dict) -> dict:
    from . import style_agents
    agent = style_agents.get(agent_id)
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    cfg = get_config(agent_id)
    pers = data.get("personality") or {}
    if pers:
        cfg["personality"].update({k: (str(pers[k])[:200] if k != "creativity" else max(0.0, min(1.0, float(pers[k]))))
                                   for k in ("tone", "response_style", "creativity") if k in pers})
        if isinstance(pers.get("expertise_areas"), list):
            cfg["personality"]["expertise_areas"] = [str(x)[:40] for x in pers["expertise_areas"]][:12]
    if "model" in data:
        model = data["model"] or {}
        if not isinstance(model, dict) or any(v and v not in MODELS for v in model.values()):
            raise ValueError(f"模型只能是 {list(MODELS)}")
        cfg["model"] = {k: v for k, v in model.items() if v}
    if "skills" in data:
        ids = _check_skills(data["skills"])
        cfg["skills"] = ids
    if "tools" in data:
        tools = data["tools"]
        if not isinstance(tools, list) or any(t not in TOOLS for t in tools):
            raise ValueError(f"工具只能是 {list(TOOLS)}")
        cfg["tools"] = tools
    if "autonomy_level" in data:
        lvl = int(data["autonomy_level"])
        if lvl not in AUTONOMY:
            raise ValueError("自主级别是 1–4")
        cfg["autonomy_level"] = lvl
    if "token_budget" in data:
        cfg["token_budget"] = max(0, int(data["token_budget"] or 0))
    if "image_backend" in data:
        if data["image_backend"] not in ("", "flux"):
            raise ValueError("生图后端只能是 flux（Qwen-Image 模型已删除）")
        cfg["image_backend"] = data["image_backend"]
    for mid, mv in (data.get("members") or {}).items():
        if mid not in cfg["members"] or not isinstance(mv, dict):
            raise ValueError(f"没有成员 {mid}")
        row = cfg["members"][mid]
        if "enabled" in mv:
            row["enabled"] = bool(mv["enabled"])
        if "model" in mv:
            if mv["model"] and mv["model"] not in MODELS:
                raise ValueError(f"模型只能是 {list(MODELS)}")
            row["model"] = mv["model"] or ""
        if "skills" in mv:
            row["skills"] = _check_skills(mv["skills"])
        if "extra_instructions" in mv:
            row["extra_instructions"] = str(mv["extra_instructions"])[:1000]
    if agent.get("kind") == "team":
        lead = agent.get("lead")
        if lead in cfg["members"] and not cfg["members"][lead]["enabled"]:
            raise ValueError("组长不能停用")
    with _lock:
        _save(_data() / agent_id / "config.json", cfg)
    return cfg


def _check_skills(ids) -> list[str]:
    if not isinstance(ids, list) or len(ids) > 40:
        raise ValueError("技能列表最多 40 条")
    known = {s["id"] for s in list_skills()}
    bad = [i for i in ids if i not in known]
    if bad:
        raise ValueError(f"技能不存在：{bad}")
    return list(dict.fromkeys(ids))


def apply_to_snapshot(agent_id: str, snap: dict) -> dict:
    """任务快照固定配置：人格、模型、技能（含全文）、工具、自主级别、预算；团队成员按配置停用 / 覆盖。"""
    cfg = get_config(agent_id)
    snap = dict(snap)
    snap["config"] = {k: cfg[k] for k in ("personality", "model", "tools", "autonomy_level", "token_budget", "image_backend")}
    snap["config"]["agent_skills"] = list(cfg["skills"])
    all_skills = {s["id"]: s for s in list_skills()}
    bound = set(cfg["skills"])
    if snap.get("members"):
        members = []
        for m in snap["members"]:
            mc = cfg["members"].get(m["id"], {})
            if not mc.get("enabled", True):
                continue
            m = dict(m)
            if mc.get("model"):
                m["model"] = mc["model"]
            if mc.get("extra_instructions"):
                m["instructions"] = m.get("instructions", "") + "\n补充要求：" + mc["extra_instructions"]
            m["skills"] = list(mc.get("skills", []))
            bound |= set(m["skills"])
            members.append(m)
        snap["members"] = members
    snap["skills"] = [_compact(all_skills[i]) for i in sorted(bound) if i in all_skills
                      and all_skills[i]["lifecycle"] != "degraded"]
    return snap


def gates_for(snap: dict) -> list[str]:
    return list(AUTONOMY_GATES.get(int((snap.get("config") or {}).get("autonomy_level", 2)), []))


def personality_text(snap: dict | None) -> str:
    p = ((snap or {}).get("config") or {}).get("personality") or {}
    if not p:
        return ""
    c = float(p.get("creativity", 0.5))
    lean = "大胆尝试新的组合" if c >= 0.7 else "稳妥、贴近已验证的做法" if c <= 0.3 else "在规则内适度创新"
    return (f"\n\nAgent 人格：语气{p.get('tone', '')}；表达{p.get('response_style', '')}；创造性 {c:.1f}（{lean}）"
            + (f"；专长：{'、'.join(p.get('expertise_areas', []))}" if p.get("expertise_areas") else "") + "。")


# ───────────────────────── 技能库 ─────────────────────────

def _compact(s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "name", "description", "instructions", "stages", "lifecycle", "icon",
                                  "required_tools", "version")}


def _norm(s: dict, source: str) -> dict:
    s = dict(s)
    s.setdefault("icon", "⚡")
    s.setdefault("category", "general")
    s.setdefault("stages", [])
    s.setdefault("required_tools", [])
    s.setdefault("lifecycle", "published" if source == "builtin" else "draft")
    s.setdefault("version", 1)
    for k in ("usage", "success", "fail"):
        s.setdefault(k, 0)
    s.setdefault("adopted_by", [])
    s.setdefault("evidence", [])
    s["source"] = s.get("source") or source
    s["effectiveness"] = round(s["success"] / s["usage"], 3) if s["usage"] else 0.0
    return s


def list_skills() -> list[dict]:
    out = {}
    for p in sorted(_builtin_dir().glob("*.json")):
        s = _norm(json.loads(p.read_text()), "builtin")
        out[s["id"]] = s
    stats = _stats()
    for sid, st in stats.items():   # 内置技能的使用统计与生命周期存在数据目录里
        if sid in out:
            out[sid].update(st)
            out[sid] = _norm(out[sid], "builtin")
    d = _skills_dir()
    if d.exists():
        for p in sorted(x for x in d.glob("*.json") if not x.name.startswith("_")):
            s = _norm(json.loads(p.read_text()), "user")
            out[s["id"]] = s
    return sorted(out.values(), key=lambda s: (LIFECYCLE.index(s["lifecycle"]) if s["lifecycle"] in LIFECYCLE else 9,
                                               s["name"]))


def get_skill(skill_id: str) -> dict:
    s = next((x for x in list_skills() if x["id"] == skill_id), None)
    if s is None:
        raise ValueError("技能不存在")
    return s


def _stats() -> dict:
    p = _skills_dir() / "_builtin_stats.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _write_skill(s: dict) -> None:
    if s["source"] == "builtin":
        st = _stats()
        st[s["id"]] = {k: s[k] for k in ("usage", "success", "fail", "lifecycle", "adopted_by", "evidence")}
        _save(_skills_dir() / "_builtin_stats.json", st)
    else:
        _save(_skills_dir() / f"{s['id']}.json", s)


def save_skill(data: dict) -> dict:
    """新建 / 修改用户技能（内置技能只能改生命周期，不能改正文）。"""
    if not isinstance(data, dict):
        raise ValueError("技能必须是对象")
    name, instr = str(data.get("name", "")).strip(), str(data.get("instructions", "")).strip()
    if not name or not instr or len(name) > 60 or len(instr) > 4000:
        raise ValueError("请填写技能名（≤60 字）和做法（≤4000 字）")
    stages = data.get("stages") or []
    if not isinstance(stages, list) or any(x not in STAGES for x in stages):
        raise ValueError(f"适用阶段只能是 {list(STAGES)}")
    tools = data.get("required_tools") or []
    if not isinstance(tools, list) or any(t not in TOOLS for t in tools):
        raise ValueError(f"依赖工具只能是 {list(TOOLS)}")
    with _lock:
        sid = data.get("id") or "sk-" + uuid.uuid4().hex[:8]
        old = next((x for x in list_skills() if x["id"] == sid), None)
        if old and old["source"] == "builtin":
            raise ValueError("内置技能正文不能修改；可以复制一份再改")
        s = _norm({**(old or {}), "id": sid, "name": name, "instructions": instr,
                   "description": str(data.get("description", ""))[:300], "stages": stages, "required_tools": tools,
                   "icon": str(data.get("icon") or "⚡")[:4], "category": str(data.get("category") or "general")[:30],
                   "version": (old or {}).get("version", 0) + 1, "updated": time.time(),
                   "origin": (old or {}).get("origin") or data.get("origin") or "manual"}, "user")
        if old:   # 正文改了：回到草稿重新验证
            s["lifecycle"] = "draft"
        _write_skill(s)
    return s


def set_lifecycle(skill_id: str, action: str) -> dict:
    s = get_skill(skill_id)
    to = {"publish": "published", "deprecate": "degraded", "restore": "published", "draft": "draft"}.get(action)
    if to is None:
        raise ValueError("动作只能是 publish / deprecate / restore / draft")
    s["lifecycle"] = to
    with _lock:
        _write_skill(s)
    return s


def delete_skill(skill_id: str) -> None:
    s = get_skill(skill_id)
    if s["source"] == "builtin":
        raise ValueError("内置技能不能删除，可以降级停用")
    with _lock:
        (_skills_dir() / f"{skill_id}.json").unlink(missing_ok=True)


def extract_from_retro(agent_id: str, run_id: str, rules: list[str], stage_hint: str = "") -> list[dict]:
    """技能萃取（精简）：团队复盘提出的规则 → 草稿技能（来源记到任务），等人审核发布。"""
    made = []
    for r in rules:
        r = str(r).strip()
        if not r or any(r == s["instructions"] for s in list_skills()):
            continue
        made.append(save_skill({"name": r[:24] + ("…" if len(r) > 24 else ""), "instructions": r,
                                "description": f"{agent_id} 团队复盘萃取（{run_id}）", "stages": [stage_hint] if stage_hint else [],
                                "origin": f"retro:{run_id}"}))
    return made


# ── 路由：BM25 近似 + 阶段匹配 + 生命周期加权 ──

def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", text)
    han = re.findall(r"[一-鿿]", text)
    return words + ["".join(han[i:i + 2]) for i in range(len(han) - 1)]   # 中文用二字片段


def route_skills(snap: dict | None, stage: str, query: str, member_id: str = "", k: int = 4) -> list[dict]:
    allowed = set(((snap or {}).get("config") or {}).get("agent_skills") or [])
    if member_id:   # 团队：本阶段负责人自己的技能 + Agent 级（全队共用）技能
        m = next((x for x in (snap or {}).get("members", []) if x["id"] == member_id), None)
        allowed |= set((m or {}).get("skills") or [])
    skills = [s for s in (snap or {}).get("skills", []) if s.get("lifecycle") != "degraded" and s["id"] in allowed]
    tools = set(((snap or {}).get("config") or {}).get("tools") or TOOLS)
    skills = [s for s in skills if set(s.get("required_tools") or []) <= tools]
    if not skills:
        return []
    docs = [_tokens(" ".join([s["name"], s.get("description", ""), s["instructions"], " ".join(s.get("stages", []))]))
            for s in skills]
    q = _tokens(query + " " + stage)
    N, avg = len(docs), sum(map(len, docs)) / len(docs) or 1
    df = Counter(t for d in docs for t in set(d))
    scored = []
    for s, d in zip(skills, docs):
        tf = Counter(d)
        bm = sum(math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.5 / (tf[t] + 1.5 * (0.25 + 0.75 * len(d) / avg))
                 for t in set(q) if t in tf)
        stage_fit = 2.0 if stage_matches(stage, s.get("stages")) else (0.5 if not s.get("stages") else -1.0)
        score = (bm + stage_fit) * LIFECYCLE_MULT.get(s.get("lifecycle", "draft"), 1.0)
        if stage_fit > 0 or bm > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [dict(s, score=round(sc, 3)) for sc, s in scored[:k]]


def skills_text(snap: dict | None, stage: str | None, query: str = "", member_id: str = "") -> str:
    if not snap or not stage:
        return ""
    picked = route_skills(snap, stage, query, member_id)
    if not picked:
        return ""
    _remember(picked, stage)
    return "\n\n本阶段调用的技能（技能库按任务与阶段检索；照做法执行，做不到的写进限制）：\n" + "\n".join(
        f"- {s['icon']} {s['name']}（{LIFECYCLE_CN.get(s['lifecycle'], '')}）：{s['instructions'][:600]}" for s in picked)


def _remember(picked: list[dict], stage: str) -> None:
    from . import teams
    ledger = teams._ledger.get()
    if ledger is None:
        return
    used = ledger.setdefault("skills_used", {})
    for s in picked:
        row = used.setdefault(s["id"], {"name": s["name"], "stages": []})
        if stage not in row["stages"]:
            row["stages"].append(stage)


def settle_skills(agent_id: str, run_id: str, used: dict, success: bool) -> list[dict]:
    """任务结算后回写技能成败，并按 Filter→Improve→Verify→Solidify 自动调整生命周期。"""
    changed = []
    with _lock:
        for sid in used or {}:
            try:
                s = get_skill(sid)
            except ValueError:
                continue
            s["usage"] += 1
            s["success" if success else "fail"] += 1
            if agent_id not in s["adopted_by"]:
                s["adopted_by"].append(agent_id)
            s["evidence"] = (s.get("evidence") or [])[-19:] + [{"run": run_id, "ok": success, "at": time.time()}]
            rate = s["success"] / s["usage"]
            before = s["lifecycle"]
            if s["usage"] >= 3 and rate < 0.4:
                s["lifecycle"] = "degraded"
            elif before == "published" and s["usage"] >= 3 and rate >= 0.7:
                s["lifecycle"] = "verified"
            elif before == "verified" and s["usage"] >= 8 and rate >= 0.8:
                s["lifecycle"] = "solidified"
            _write_skill(_norm(s, s["source"]))
            if before != s["lifecycle"]:
                changed.append({"id": sid, "name": s["name"], "from": before, "to": s["lifecycle"]})
    return changed
