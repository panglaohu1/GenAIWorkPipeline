"""Oskar 导演：多场景世界的目标图、逐场景趋近与世界整合。

Oskar 在这里不是一个“风格提示词”，而是整条流水线的导演：
  1. 世界圣经   —— 先定世界（支柱、色彩角色、共享格网/层高/海面、旅程节奏），再定每一站；
  2. 目标图     —— 每个场景用同一套风格锁出图，Oskar 按导演档案的评审维度逐张打分、选图，不达标写美术指导重画；
  3. 逐场景趋近 —— 按“修硬伤 → 定大形 → 理层次 → 收色彩与世界”的阶段推动受限编辑，
                   每轮给维度分（导演分）+ 客观相似度，按规则接受 / 回滚，收敛或停滞即停；
  4. 世界整合   —— 按旅程把场景排成群岛（高潮在弧线顶点），只调位置、间距与朝向，Oskar 复评整体。

评审维度、阶段、阈值与整合原则都来自 Agent 定义里的 direction（agents/oskar.json），任务开始时随风格快照固定。
导演分是 VLM 依锚点打出的主观分：接受规则仍以硬检查和客观相似度为先，导演分只在客观持平时决定、
或在客观上升时否决明显变差的修改。所有判断都保存证据，不把静帧当成通行或动画已验证。

本模块的纯函数（校验、阶段、接受规则、布局、世界检查）不依赖 LLM / Blender，可单测；
LLM 调用集中在 world_bible / judge_targets / critique / world_review，converge 通过注入的 evaluate / critique 运行。
"""
from __future__ import annotations

import copy
import itertools
import json
import math
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import vision
from ..llm import LLM
from . import prompts as P
from .program import validate_program

ROLES = {"opening": "开场", "rise": "上升", "climax": "高潮", "resolve": "收束", "interlude": "间奏"}
LINKS = {"auto": "按地势定", "terraces": "阶梯台地", "ridge": "山脊垭口", "isthmus": "低地地峡",
         "bridge": "桥", "harbour": "港口", "stairs": "台阶", "ferry": "渡船", "none": "无"}
SCENE_TYPES = {"sea_stacks", "citadel", "town", "fantasy", "other"}
STOP_REASONS = {"converged": "已收敛（导演分、硬检查与结构相似度都过线）", "plateau": "停滞（连续多轮没有被接受的修改）",
                "max_rounds": "达到最大轮数", "no_edits": "导演没有给出可执行的修改", "build_failed": "初稿构建失败",
                "director_done": "导演判断已经成立，主动收工"}
ACTIONS = {"edit": "受限修改", "replan": "推翻重写场景程序", "done": "宣布完成"}


def _default_direction() -> dict:
    path = Path(__file__).resolve().parents[3] / "agents" / "oskar.json"
    try:
        return json.loads(path.read_text()).get("direction") or {}
    except (OSError, ValueError):
        return {}


def resolve_direction(direction: dict | None) -> dict:
    """任务快照里的导演档案；旧任务或缺字段时用 Oskar 内置档案补齐。"""
    base = _default_direction()
    d = copy.deepcopy(direction or {})
    for key, value in base.items():
        d.setdefault(key, copy.deepcopy(value))
    conv = dict(base.get("convergence") or {})
    conv.update((direction or {}).get("convergence") or {})
    d["convergence"] = conv
    return d


# ───────────────────────────── 评审维度 ─────────────────────────────

def rubric(direction: dict, key: str = "rubric") -> list[dict]:
    return list(resolve_direction(direction).get(key) or [])


def rubric_text(dims: list[dict]) -> str:
    lines = []
    for d in dims:
        anchors = "；".join(f"{k} 分 = {v}" for k, v in sorted((d.get("anchors") or {}).items()))
        lines.append(f"- {d['id']}（{d['name']}，权重 {d.get('weight', 0):.2f}）：{d.get('ask', '')}"
                     + (f"\n  锚点：{anchors}" if anchors else ""))
    return "\n".join(lines)


def clamp_scores(raw: Any, dims: list[dict]) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out = {}
    for d in dims:
        v = raw.get(d["id"])
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return None
        out[d["id"]] = round(max(0.0, min(10.0, float(v))), 2)
    return out


def weighted(scores: dict | None, dims: list[dict]) -> float | None:
    if not scores:
        return None
    total = sum(float(d.get("weight", 0)) for d in dims) or 1.0
    return round(sum(scores.get(d["id"], 0.0) * float(d.get("weight", 0)) for d in dims) / total, 2)


def persona(direction: dict, style_rules: str = "") -> str:
    d = resolve_direction(direction)
    motto = " ".join(d.get("motto") or [])
    return ("你是「Oskar 方法导演」——受 Oskar Stålberg 公开创作方法启发的研究型 Agent（不是本人，也不代表其意见）。"
            "你带领一个确定性世界引擎（不规则格网 + WFC 模块 + 程序化岩体与建筑）建造一个由多个场景组成的世界，"
            "对每一站的目标图、每一轮修改和最后的整合负责。\n"
            f"信条：{motto}\n"
            "工作习惯：\n"
            "- 先看大形：剪影、主峰与支脉、台面是否有真实面积；大形不成立时不谈细节。\n"
            "- 你只决定有什么、在哪、多大、多高、谁主谁次；窗、城垛、岩纹、植被细节交给引擎的规则生长。\n"
            "- 颜色服务空间：语义色角色有限、明度分主次；同一个世界只有一套色彩角色、一种格网与层高。\n"
            "- 每次只改最影响的一两处，改完让证据说话：硬检查与客观相似度优先，你的打分只在它们持平时起决定作用。\n"
            "- 诚实：静帧不能证明通行与动画；研究里尚未移植到引擎的技术不能当作已有能力。\n"
            f"说话方式：{d.get('voice', '')}" + style_rules)


# ───────────────────────────── 世界圣经 ─────────────────────────────

BIBLE = """为下面的需求设计一个由多个场景组成的世界：默认 {n} 个场景（可在 {n_min}–{n_max} 之间按需求取舍）。先定世界，再定每一站。
需求：{brief}

引擎能做的（场景只能由这些构成）：岩柱 / 崖体 / 台地、海与岛内湖、主楼 / 翼楼 / 塔楼 / 房屋（平顶、坡顶、穹顶、尖顶）、
城墙与门洞、通路与台阶、石拱桥与细轨高架、瀑布、晶柱簇、大冠树与成片植被。
不能做：人物、动物、室内、车辆、文字、写实材质。每个场景是一块圆形海盘上的小岛 diorama。

输出 JSON：
{{"title": "世界名",
 "vision": "你对这个世界的一句话（中文，像导演的开场白）",
 "pillars": ["2–4 条世界支柱，每条一句中文：大形 / 色彩 / 节奏方面的承诺"],
 "art_style": "English style keywords shared by every scene",
 "palette": {{"base": "citadel" | "sea_stack" | "auto", "roles": "English colour roles shared by every scene",
             "notes": "中文：每个语义色角色是什么、明度主次"}},
 "shared": {{"sea_level": 0–2 的整数（全部场景共用的海面层）}},
 "world_form": "planet"（所有场景贴在一颗小行星上，相邻站之间由地势连起来，不搭桥；默认）| "archipelago"（平面群岛）,
 "scenes": [{{"id": "snake_case", "name": "中文名", "role": "opening" | "rise" | "climax" | "resolve" | "interlude",
   "intensity": 1–5 的整数（旅程张力，高潮为 5）, "scene_type": "sea_stacks" | "citadel" | "town" | "fantasy" | "other",
   "dominant": "主体是什么", "silhouette": "剪影一句话", "composition": "构图：主体在画面哪里、前中远景",
   "must_have": ["引擎能做的元素"], "avoid": ["要避免的"], "grid_radius": 6–9 的整数（高潮可更大）,
   "link_to_next": "auto" | "terraces" | "ridge" | "isthmus"（星球：用地势连到下一站——阶梯台地 / 山脊垭口 / 低地地峡；auto 由整合时看两端岛缘再定）| "bridge" | "harbour" | "ferry"（仅平面群岛）,
   "key_art_prompt": "English: ONE diorama island scene — landforms, buildings, water, composition; no characters, no style words"}}]}}
规则：scenes 按旅程顺序排列；第一个是 opening；恰好一个 climax，且它的 intensity 最高；每个场景只有一个主体；
相邻场景要有变化（主体类型或高度），整体有起伏。key_art_prompt 不写风格词（系统会给所有场景加同一套风格锁）。只输出 JSON。"""


def validate_bible(o: Any, n_min: int = 2, n_max: int = 6) -> str | None:
    if not isinstance(o, dict):
        return "需要 JSON 对象"
    for k in ("title", "vision", "art_style", "scenes"):
        if not o.get(k):
            return f"缺少字段 {k}"
    if not isinstance(o.get("pillars"), list) or not 1 <= len(o["pillars"]) <= 5:
        return "pillars 需要 1–5 条"
    pal = o.get("palette")
    if not isinstance(pal, dict) or not pal.get("roles"):
        return "palette 需要 {base, roles, notes}"
    scenes = o["scenes"]
    if not isinstance(scenes, list) or not n_min <= len(scenes) <= n_max:
        return f"scenes 需要 {n_min}–{n_max} 个场景"
    ids = set()
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            return f"scenes[{i}] 需要对象"
        for k in ("id", "name", "role", "dominant", "key_art_prompt"):
            if not s.get(k):
                return f"scenes[{i}] 缺少 {k}"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", str(s["id"])):
            return f"scenes[{i}].id 需要 snake_case（小写字母开头）"
        if s["id"] in ids:
            return f"场景 id 重复：{s['id']}"
        ids.add(s["id"])
        if s["role"] not in ROLES:
            return f"scenes[{i}].role 必须是 {list(ROLES)} 之一"
        if not isinstance(s.get("intensity"), (int, float)) or not 1 <= s["intensity"] <= 5:
            return f"scenes[{i}].intensity 需要 1–5"
    climax = [s for s in scenes if s["role"] == "climax"]
    if len(climax) != 1:
        return "需要恰好一个 climax 场景"
    if climax[0]["intensity"] < max(s["intensity"] for s in scenes):
        return "climax 场景的 intensity 必须最高"
    return None


def _text(v) -> str:
    """模型偶尔把应是字符串的字段写成数组（如 palette.roles: ["limestone grey", ...]）：统一拼成一句。"""
    if isinstance(v, (list, tuple)):
        return ", ".join(_text(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_text(x)}" for k, x in v.items())
    return "" if v is None else str(v)


TEXT_FIELDS = ("title", "vision", "art_style")
SCENE_TEXT = ("name", "dominant", "silhouette", "composition", "key_art_prompt")


def coerce_text(b: dict) -> dict:
    """世界圣经里的文本字段统一成字符串（已保存的旧圣经重跑时也走这里）。"""
    for k in TEXT_FIELDS:
        if k in b:
            b[k] = _text(b[k])
    pal = b.get("palette")
    if isinstance(pal, dict):
        for k in ("roles", "notes"):
            if k in pal:
                pal[k] = _text(pal[k])
    b["pillars"] = [_text(x) for x in b.get("pillars") or []]
    for s in b.get("scenes") or []:
        for k in SCENE_TEXT:
            if k in s:
                s[k] = _text(s[k])
        for k in ("must_have", "avoid"):
            s[k] = [_text(x) for x in s.get(k) or []] if isinstance(s.get(k), list) else ([_text(s[k])] if s.get(k) else [])
    return b


def normalize_bible(o: dict) -> dict:
    b = coerce_text(copy.deepcopy(o))
    pal = b.setdefault("palette", {})
    if pal.get("base") not in ("citadel", "sea_stack", "auto"):
        pal["base"] = "auto"
    shared = b.setdefault("shared", {})
    sea = shared.get("sea_level", 1)
    shared["sea_level"] = int(sea) if isinstance(sea, (int, float)) and 0 <= sea <= 3 else 1
    shared["cell"], shared["level_h"] = 2.0, 2.0   # 同一世界同一格网与层高（尺度一致是整合的硬检查）
    for s in b["scenes"]:
        s["intensity"] = int(s["intensity"])
        r = s.get("grid_radius")
        s["grid_radius"] = int(r) if isinstance(r, (int, float)) and 5 <= r <= 10 else (9 if s["role"] == "climax" else 7)
        if s.get("scene_type") not in SCENE_TYPES:
            s["scene_type"] = "other"
        if s.get("link_to_next") not in LINKS:
            s["link_to_next"] = "auto"
        for k in ("must_have", "avoid"):
            s[k] = [str(x) for x in s.get(k) or []][:8]
    b["scenes"][-1]["link_to_next"] = "none"
    if b.get("world_form") not in ("planet", "archipelago"):
        b["world_form"] = "planet"
    return b


def anchor_id(bible: dict) -> str:
    """世界母题：高潮场景先出图，其余场景以它为一致性锚点。"""
    return next(s["id"] for s in bible["scenes"] if s["role"] == "climax")


def target_order(bible: dict) -> list[dict]:
    a = anchor_id(bible)
    return sorted(bible["scenes"], key=lambda s: s["id"] != a)


async def world_bible(brief: str, *, model: str, direction: dict, style_rules: str = "", n: int = 3,
                      n_min: int = 2, n_max: int = 6, refs: list[str] | None = None, on_delta=None) -> dict:
    from ..llm import user_message
    n = max(n_min, min(n_max, n))
    msg = user_message(BIBLE.format(n=n, n_min=n_min, n_max=n_max, brief=brief), refs or None)
    o = await LLM(model).json([{"role": "system", "content": persona(direction, style_rules)}, msg],
                              thinking_budget=4096, on_delta=on_delta, validate=lambda x: validate_bible(x, n_min, n_max))
    return normalize_bible(o)


# ───────────────────────────── 目标图 ─────────────────────────────

def target_prompt(bible: dict, scene: dict, direction: dict, content: str | None = None) -> str:
    """风格锁：同一个世界的所有场景共用同一串风格词与色彩角色，只换场景内容。"""
    d = resolve_direction(direction)
    return ", ".join(x for x in (content or scene["key_art_prompt"], bible.get("art_style", ""),
                                  (bible.get("palette") or {}).get("roles", ""), d.get("target_style_lock", "")) if x)


JUDGE = """你在为世界《{title}》的场景「{name}」（{role}，张力 {intensity}/5）挑选目标图。这张图会交给引擎复刻：既要好看，也要能用引擎原语建出来。
世界的一句话：{vision}
世界支柱：{pillars}
色彩角色：{palette}
这一站的要求：主体 {dominant}；剪影 {silhouette}；构图 {composition}；必须有 {must}；避免 {avoid}。
{consistency}
按以下维度给每张候选打分（0–10，按锚点校准；7 分以上必须真的成立，不要客气）：
{rubric}
另判断 buildable：图中主体能否用引擎原语表达（人物、室内、写实细节、悬浮结构都算不能）。
输出 JSON：{{"candidates": [{{"index": 序号, "scores": {{"维度id": 分}}, "buildable": true 或 false, "note": "一句话"}}],
 "best": 序号, "direction": "给这一站的一句美术指导（中文）",
 "action": "accept"（最好的一张可以交给引擎）| "redraw"（都不够好，你要重画）,
 "revised_prompt": "选 redraw 时用于重画的英文场景描述（只写内容，不写风格词）"}}"""


def _judge_ok(o: Any, n: int, dims: list[dict]) -> str | None:
    if not isinstance(o, dict) or not isinstance(o.get("candidates"), list):
        return "需要 {candidates: [...], best, direction, revised_prompt}"
    seen = set()
    for c in o["candidates"]:
        if not isinstance(c, dict) or not isinstance(c.get("index"), int) or not 0 <= c["index"] < n:
            return f"candidates[].index 必须是 0..{n - 1}"
        if clamp_scores(c.get("scores"), dims) is None:
            return "每张候选需要全部维度的数值分：" + "、".join(d["id"] for d in dims)
        seen.add(c["index"])
    if len(seen) != n:
        return f"需要给全部 {n} 张候选打分"
    return None


def pick_target(judged: list[dict], consistency: list[float] | None = None) -> int:
    """导演分最高者；不可建的扣 2 分；与锚点风格一致性明显偏离（SigLIP 低于最好者 0.05 以上）的最多扣 1 分。"""
    def key(i: int) -> float:
        c = judged[i]
        s = (c.get("director") or 0) - (0 if c.get("buildable", True) else 2.0)
        if consistency:
            s += max(-1.0, min(0.0, (consistency[i] - max(consistency)) * 20))
        return s
    return max(range(len(judged)), key=key)


async def judge_targets(candidates: list[str], *, bible: dict, scene: dict, direction: dict, model: str,
                        anchor: str | None = None, style_rules: str = "", on_delta=None) -> dict:
    dims = rubric(direction)
    consistency = None
    if anchor:
        m = await vision.objective_similarity(anchor, candidates)
        if m and len(m.get("siglip_per") or []) == len(candidates):
            consistency = [round(float(x), 3) for x in m["siglip_per"]]
    note = ("与世界母题（高潮场景已确认的目标图，附在最后）比较风格一致性：同一套色彩角色、格网感与光照。"
            if anchor else "这是世界母题（第一张目标图）：它确定的色彩与尺度会约束其余场景。")
    groups = [(f"候选 {i}:", [p]) for i, p in enumerate(candidates)]
    if anchor:
        groups.append(("世界母题（参考，不打分）:", [anchor]))
    text = JUDGE.format(title=bible["title"], name=scene["name"], role=ROLES.get(scene["role"], scene["role"]),
                        intensity=scene["intensity"], vision=bible.get("vision", ""),
                        pillars="；".join(bible.get("pillars", [])), palette=(bible.get("palette") or {}).get("notes", ""),
                        dominant=scene.get("dominant", ""), silhouette=scene.get("silhouette", ""),
                        composition=scene.get("composition", ""), must="、".join(scene.get("must_have", [])) or "—",
                        avoid="、".join(scene.get("avoid", [])) or "—", consistency=note, rubric=rubric_text(dims))
    msg = vision._vision_msg(text, groups, 640)
    o = await LLM(model).json([{"role": "system", "content": persona(direction, style_rules)}, msg],
                              thinking_budget=3072, on_delta=on_delta,
                              validate=lambda x: _judge_ok(x, len(candidates), dims))
    judged = [None] * len(candidates)
    for c in o["candidates"]:
        scores = clamp_scores(c["scores"], dims)
        judged[c["index"]] = {"scores": scores, "director": weighted(scores, dims),
                              "buildable": c.get("buildable", True) is not False, "note": str(c.get("note", ""))[:300]}
    if consistency:
        for j, v in zip(judged, consistency):
            j["consistency"] = v
    best = pick_target(judged, consistency)
    return {"candidates": judged, "best": best, "llm_best": o.get("best"), "direction": str(o.get("direction", ""))[:400],
            "action": "redraw" if o.get("action") == "redraw" else "accept",
            "revised_prompt": str(o.get("revised_prompt", ""))[:800], "consistency": consistency}


# ───────────────────────────── 场景程序约束 ─────────────────────────────

def scene_rules(bible: dict, scene: dict) -> str:
    sh = bible["shared"]
    idx = [s["id"] for s in bible["scenes"]].index(scene["id"])
    return (f"\n\n世界《{bible['title']}》第 {idx + 1}/{len(bible['scenes'])} 站「{scene['name']}」"
            f"（{ROLES.get(scene['role'], scene['role'])}，张力 {scene['intensity']}/5）。导演要求：\n"
            f"- 主体：{scene.get('dominant', '')}；剪影：{scene.get('silhouette', '')}；构图：{scene.get('composition', '')}\n"
            f"- 必须有：{'、'.join(scene.get('must_have', [])) or '—'}；避免：{'、'.join(scene.get('avoid', [])) or '—'}\n"
            f"- 全世界共用：grid.cell {sh['cell']}、level_h {sh['level_h']}、sea_level {sh['sea_level']}；"
            f"grid.radius {scene['grid_radius']}（系统会强制写入，不要改）\n"
            f"- 世界色彩角色：{(bible.get('palette') or {}).get('notes', '')}")


def lock_program(program: dict, bible: dict, scene: dict, palette: Any = None, palette_base: str | None = None) -> dict:
    """把世界共享的格网、层高、海面与色彩写进场景程序：尺度和色彩一致由系统保证，而不是靠 LLM 记住。"""
    p = copy.deepcopy(program)
    sh = bible["shared"]
    grid = dict(p.get("grid") or {})
    grid.update(cell=sh["cell"], radius=scene["grid_radius"])
    p["grid"], p["level_h"] = grid, sh["level_h"]
    if p.get("sea_level") is not None or scene.get("scene_type") == "sea_stacks":
        p["sea_level"] = sh["sea_level"]
    if palette is not None:
        p["palette"] = copy.deepcopy(palette)
        if palette_base:
            p["palette_base"] = palette_base
    p["scene"] = p.get("scene") or scene["name"]
    return p


# ───────────────────────────── 逐场景趋近 ─────────────────────────────

CRITIQUE = """世界《{title}》第 {index} 站「{name}」（{role}）。左边第一张是已确认的目标图，后面是引擎当前渲染（同一构图的不同机位）。
{metrics}
本轮阶段：【{phase_name}】{phase_focus}
{failed}
当前场景程序（item 序号用于编辑）：
{program}

先按维度给当前渲染打分（0–10，按锚点校准；引擎细节如窗、城垛、岩纹不扣分，只看维度本身）：
{rubric}
再给出最多 3 条最影响「像不像、成不成立」的修改，必须属于本轮阶段，每条是一个扁平对象并直接带编辑操作：
- move：{{"what": "差异", "op": "move", "item": 序号, "dx": ±0.02–0.15, "dy": ±0.02–0.15}}
- scale：{{"what": "差异", "op": "scale", "item": 序号, "factor": 0.7–1.4}}
- top：{{"what": "差异", "op": "top", "item": 序号, "tier": 序号或 -1, "delta": ±1–4}}
- add_tier：{{"what": "差异", "op": "add_tier", "item": 序号, "r": 半径, "top": 层}}
- remove：{{"what": "差异", "op": "remove", "item": 序号}}
- add：{{"what": "差异", "op": "add", "item_json": {{完整原语}}}}
最后由你决定下一步动作 action：
- "edit"：按上面的 diffs 做受限修改（默认）；
- "replan"：大形根本不对、小修救不回来时，推翻当前程序，重新读图写一版（{replan_note}）；
- "done"：你确信这一站已经成立、再改只会更糟（硬检查必须全部通过）。
{journal}
只输出 JSON：{{"scores": {{"维度id": 分}}, "verdict": "一句导演意见", "keep": ["已经成立、不要动的"],
 "action": "edit" | "replan" | "done", "diffs": [ {{...}} ]}}"""


def phase_for(checks_ok: bool, scores: dict | None, direction: dict) -> dict:
    """先修硬伤 → 定大形 → 理层次 → 收色彩与世界：哪一阶段的维度还没过阶段线，就停在哪一阶段。"""
    d = resolve_direction(direction)
    phases = d.get("phases") or []
    fix = next((p for p in phases if p.get("when") == "hard_fail"), None)
    if not checks_ok and fix:
        return fix
    ordered = [p for p in phases if p.get("dims")]
    if not ordered:
        return {"id": "form", "name": "定大形", "focus": ""}
    if not scores:
        return ordered[0]
    floor = float(d.get("phase_floor", 6.0))
    for p in ordered:
        if min(scores.get(k, 0.0) for k in p["dims"]) < floor:
            return p
    return ordered[-1]


def decide(cand: dict, best: dict, conv: dict) -> tuple[bool, str]:
    """接受规则（硬检查 > 客观相似度 > 导演分）：
    - 构建失败 → 拒绝；硬检查状态变化 → 修好接受、引入硬伤拒绝；
    - 客观指数升 ≥ obj_gain 且导演分不降超过 max_director_drop → 接受；
    - 客观持平（±obj_gain 内）时导演分升 ≥ director_gain → 接受（大形判断胜出）；其余拒绝。"""
    if not cand.get("ok"):
        return False, f"构建失败：{str(cand.get('error') or '')[:80]}"
    if bool(cand.get("checks_ok")) != bool(best.get("checks_ok")):
        return (True, "修好了硬检查") if cand.get("checks_ok") else (False, "引入了新的硬检查失败")
    gain, tie = float(conv.get("obj_gain", 0.3)), float(conv.get("director_gain", 1.0))
    drop = float(conv.get("max_director_drop", 0.5))
    d_obj = (cand.get("objective") or 0.0) - (best.get("objective") or 0.0) \
        if cand.get("objective") is not None and best.get("objective") is not None else 0.0
    cd, bd = cand.get("director"), best.get("director")
    d_dir = (cd - bd) if cd is not None and bd is not None else 0.0
    if d_obj >= gain:
        if d_dir < -drop:
            return False, f"客观 +{d_obj:.2f}，但导演分 {d_dir:+.2f}：像了，却不成立"
        return True, f"客观相似度 +{d_obj:.2f}，导演分 {d_dir:+.2f}"
    if abs(d_obj) < gain and d_dir >= tie:
        return True, f"客观持平（{d_obj:+.2f}），导演分 +{d_dir:.2f}：大形判断胜出"
    return False, f"未提升：客观 {d_obj:+.2f}，导演分 {d_dir:+.2f}"


def converged(best: dict, conv: dict, min_dino: float | None = None) -> bool:
    if not best.get("checks_ok") or best.get("director") is None:
        return False
    if best["director"] < float(conv.get("scene_pass", 7.0)):
        return False
    m = best.get("metrics")
    return m is None or min_dino is None or m.get("dino", 0) >= min_dino


def forecast(history: list[dict], conv: dict) -> dict:
    acc = [h for h in history if h.get("accepted") and h.get("director") is not None]
    if not acc:
        return {}
    first, last = acc[0], acc[-1]
    span = max(1, last["round"] - first["round"])
    rate = round((last["director"] - first["director"]) / span, 2) if len(acc) > 1 else 0.0
    gap = round(max(0.0, float(conv.get("scene_pass", 7.0)) - last["director"]), 2)
    stale = sum(1 for _ in itertools.takewhile(lambda h: not h.get("accepted"), reversed(history)))
    return {"director": last["director"], "gap": gap, "rate": rate, "stale": stale,
            "eta_rounds": math.ceil(gap / rate) if rate > 0 and gap > 0 else (0 if gap == 0 else None)}


def _edits(crit: dict | None) -> list[dict]:
    out = []
    for d in (crit or {}).get("diffs") or []:
        if isinstance(d, dict):
            e = d.get("edit") if isinstance(d.get("edit"), dict) else d
            if e.get("op"):
                out.append(e)
    return out[:3]


async def converge(program: dict, *, evaluate: Callable[[dict, str], Awaitable[dict]],
                   critique: Callable[[dict, dict, list[dict]], Awaitable[dict | None]], direction: dict,
                   max_rounds: int | None = None, min_dino: float | None = None,
                   on_round: Callable[[dict], None] | None = None,
                   checkpoint: Callable[[], Awaitable[None]] | None = None,
                   replan: Callable[[dict, list[dict]], Awaitable[dict]] | None = None,
                   max_replans: int = 1) -> dict:
    """Oskar 推动一个场景趋近目标。evaluate(program, tag) → 构建/检查/渲染/客观指标；
    critique(record, phase, failed_attempts) → {scores, verdict, keep, action, diffs}（失败返回 None）；
    replan(best_record, failed_attempts) → 新程序：导演选择 "replan"、或停滞时还有一次推倒重来的机会。
    每一轮的动作都由导演决定：edit（受限修改）/ replan（重写程序）/ done（宣布完成，需硬检查全过）。"""
    d = resolve_direction(direction)
    conv = d["convergence"]
    dims = rubric(d)
    max_rounds = int(conv.get("max_rounds", 4) if max_rounds is None else max_rounds)
    patience = max(1, int(conv.get("patience", 2)))

    replans = 0

    async def judge(rec: dict, failed: list[dict]) -> None:
        phase = phase_for(bool(rec.get("checks_ok")), (rec.get("critique") or {}).get("scores") or rec.get("_prev_scores"), d)
        phase = {**phase, "can_replan": replan is not None and replans < max_replans}
        try:
            crit = await critique(rec, phase, failed)
        except Exception as e:   # 评审失败不终止：没有导演分时只按硬检查与客观指标判断
            crit = {"error": str(e)[:200]}
        scores = clamp_scores((crit or {}).get("scores"), dims)
        rec["critique"] = {**(crit or {}), "scores": scores}
        rec["director"] = weighted(scores, dims)
        rec["phase"] = {"id": phase.get("id"), "name": phase.get("name")}

    history: list[dict] = []
    first = await evaluate(program, "r0")
    first.update(round=0, program=program, accepted=True, decision="初稿")
    if not first.get("ok"):
        history.append(first)
        return {"history": history, "best": first, "stop_reason": "build_failed", "forecast": {}}
    await judge(first, [])
    history.append(first)
    if on_round:
        on_round(first)
    best, stale, failed = first, 0, []
    stop = "max_rounds"
    for rnd in range(1, max_rounds + 1):
        if checkpoint:
            await checkpoint()
        if converged(best, conv, min_dino):
            stop = "converged"
            break
        action = (best.get("critique") or {}).get("action") or "edit"
        if action == "done" and best.get("checks_ok"):
            stop = "director_done"
            break
        can_replan = replan is not None and replans < max_replans
        if stale >= patience:
            if not can_replan:
                stop = "plateau"
                break
            action = "replan"          # 停滞时导演还有一次推倒重来的机会
        edits: list[dict] = []
        if action == "replan" and can_replan:
            replans += 1
            try:
                newp, elog = await replan(best, failed), ["replan：推翻当前程序，重新读图写一版"]
            except Exception as e:
                newp, elog = best["program"], [f"replan 失败：{str(e)[:80]}"]
            stale = 0
        else:
            action = "edit"
            edits = _edits(best.get("critique"))
            if not edits:
                await judge(best, failed + [{"reason": "上一轮没有给出可执行的编辑，请给出至少一条"}])
                edits = _edits(best.get("critique"))
                if not edits:
                    stop = "no_edits"
                    break
            newp, elog = P.apply_edits(best["program"], edits)
        err = validate_program(newp)
        if err:
            rec = {"round": rnd, "program": newp, "edit_log": elog, "error": err, "accepted": False,
                   "decision": f"编辑后程序无效：{err}", "phase": best.get("phase")}
        else:
            rec = await evaluate(newp, f"r{rnd}")
            rec.update(round=rnd, program=newp, edit_log=elog, _prev_scores=(best.get("critique") or {}).get("scores"))
            if rec.get("ok"):
                await judge(rec, failed)
            rec.pop("_prev_scores", None)
            rec["accepted"], rec["decision"] = decide(rec, best, conv)
        rec["edits"] = edits
        rec["action"] = action
        history.append(rec)
        if on_round:
            on_round(rec)
        if rec["accepted"]:
            best, stale, failed = rec, 0, []
        else:
            stale += 1
            failed.append({"edits": elog, "reason": rec["decision"]})
            if stale < patience and rnd < max_rounds:
                await judge(best, failed)   # 回到当前最佳，带着失败尝试重新给方向
    else:
        if converged(best, conv, min_dino):
            stop = "converged"
    return {"history": history, "best": best, "stop_reason": stop, "forecast": forecast(history, conv),
            "lessons": lessons(history)}


def lessons(history: list[dict]) -> dict:
    """工具使用经验（确定性统计，不是风格规则）：每种编辑操作 / 动作被尝试与被接受的次数，各阶段的接受情况。"""
    ops: dict[str, dict] = {}
    phases: dict[str, dict] = {}
    for h in history[1:]:
        acc = bool(h.get("accepted"))
        kinds = ["replan"] if h.get("action") == "replan" else [e.get("op") for e in h.get("edits") or [] if isinstance(e, dict)]
        for k in kinds or ["(无)"]:
            o = ops.setdefault(str(k), {"tried": 0, "accepted": 0})
            o["tried"] += 1
            o["accepted"] += acc
        ph = ((h.get("phase") or {}).get("name")) or "-"
        p = phases.setdefault(ph, {"tried": 0, "accepted": 0})
        p["tried"] += 1
        p["accepted"] += acc
    rejects = [h.get("decision") for h in history[1:] if not h.get("accepted") and h.get("decision")]
    return {"ops": ops, "phases": phases, "rejections": rejects[-5:],
            "gain": round(max((h.get("director") or 0) for h in history if h.get("accepted")) - (history[0].get("director") or 0), 2)
            if history and history[0].get("accepted") else 0}


def merge_lessons(entries: list[dict]) -> dict:
    total: dict[str, dict] = {}
    for e in entries:
        for k, v in (e.get("ops") or {}).items():
            t = total.setdefault(k, {"tried": 0, "accepted": 0})
            t["tried"] += int(v.get("tried", 0))
            t["accepted"] += int(v.get("accepted", 0))
    return total


def journal_text(entries: list[dict], title: str = "工作室手记") -> str:
    """把经验写成导演能读的一段话：哪些改法在这个世界里有效，哪些反复被回滚。"""
    if not entries:
        return ""
    ops = merge_lessons(entries)
    lines = [f"{title}（你自己在前面工作中积累的工具使用经验；是统计，不是风格规则）："]
    if ops:
        lines.append("- 改法接受率：" + "，".join(f"{k} {v['accepted']}/{v['tried']}" for k, v in
                                               sorted(ops.items(), key=lambda kv: -kv[1]["tried"])))
    for e in entries[-4:]:
        if e.get("note"):
            lines.append(f"- {e.get('scene', '')}：{e['note']}")
        for r in (e.get("rejections") or [])[-2:]:
            lines.append(f"  · 被回滚：{r}")
    lines.append("据此优先使用接受率高的改法；接受率低的改法换一种做法再用。")
    return "\n".join(lines)


async def critique(rec: dict, phase: dict, failed: list[dict], *, target: str, bible: dict, scene: dict,
                   direction: dict, model: str, style_rules: str = "", on_delta=None, journal: str = "",
                   can_replan: bool = True) -> dict:
    dims = rubric(direction)
    mt = ""
    if rec.get("metrics"):
        mt = f"客观相似度 DINO {rec['metrics']['dino']:.3f} / SigLIP {rec['metrics']['siglip']:.3f}。"
    fails = [c for c in (rec.get("checks") or []) if c.get("required") and not c["passed"]]
    if fails:
        mt += "硬检查未过（本轮必须先修）：" + "；".join(f"{c['name']} {c.get('detail', '')}" for c in fails)
    if rec.get("notes"):
        mt += " 引擎自动修正：" + "；".join(rec["notes"][:4])
    ftxt = ""
    if failed:
        ftxt = "已经试过但没被接受的修改（换一种改法，不要重复）：\n" + "\n".join(
            f"- {'、'.join(f.get('edits') or []) or '—'} → {f.get('reason', '')}" for f in failed[-3:])
    numbered = json.dumps([{"item": i, **it} for i, it in enumerate(rec["program"]["items"])], ensure_ascii=False)
    idx = [s["id"] for s in bible["scenes"]].index(scene["id"]) + 1
    text = CRITIQUE.format(title=bible["title"], index=idx, name=scene["name"], role=ROLES.get(scene["role"], scene["role"]),
                           metrics=mt, phase_name=phase.get("name", ""), phase_focus=phase.get("focus", ""), failed=ftxt,
                           program=numbered, rubric=rubric_text(dims), journal=journal,
                           replan_note="本站还可以用一次" if can_replan else "本站已用过，不要再选")
    msg = vision._vision_msg(text, [("目标图：", [target]), ("当前渲染：", (rec.get("images") or [])[:2])], 768)

    def ok(o):
        if not isinstance(o, dict) or not isinstance(o.get("diffs"), list):
            return "需要 {scores, verdict, keep, diffs: [...]}"
        if clamp_scores(o.get("scores"), dims) is None:
            return "scores 需要全部维度的数值分：" + "、".join(x["id"] for x in dims)
        if o.get("action", "edit") not in ACTIONS:
            return "action 必须是 edit / replan / done"
        return None
    return await LLM(model).json([{"role": "system", "content": persona(direction, style_rules)}, msg],
                                 thinking_budget=2048, retries=2, validate=ok, on_delta=on_delta)


# ───────────────────────────── 世界整合 ─────────────────────────────

def peak_level(program: dict) -> int:
    top = 0
    for it in program.get("items", []):
        for t in it.get("tiers") or []:
            top = max(top, int(t.get("top", 0)))
        for k in ("top", "level"):
            if isinstance(it.get(k), (int, float)):
                top = max(top, int(it[k]))
    return top


def world_checks(programs: dict[str, dict], bible: dict, palette_locked: bool = True) -> list[dict]:
    """确定性的世界检查：尺度一致（必需）、色彩角色统一（锁定时必需）、节奏（高潮最高，建议）。"""
    checks = []
    scales = {(float((p.get("grid") or {}).get("cell", 2.0)), float(p.get("level_h", 2.0))) for p in programs.values()}
    checks.append({"name": "尺度一致（格网 / 层高）", "passed": len(scales) == 1, "required": True,
                   "detail": "、".join(f"cell {c} / 层高 {h}" for c, h in sorted(scales))})
    if palette_locked:
        pals = {json.dumps(p.get("palette"), sort_keys=True) for p in programs.values()}
        checks.append({"name": "色彩角色统一", "passed": len(pals) == 1, "required": True,
                       "detail": f"{len(pals)} 套调色盘"})
    peaks = {sid: peak_level(p) for sid, p in programs.items()}
    climax = anchor_id(bible)
    if climax in peaks and peaks:
        top = max(peaks.values())
        checks.append({"name": "节奏：高潮场景最高", "passed": peaks[climax] >= top, "required": False,
                       "detail": "，".join(f"{sid} {v} 层" for sid, v in peaks.items())})
    return checks


def layout(order: list[str], sizes: dict[str, list[float]], *, climax: str | None = None, gap: float = 8.0,
           spacing: float = 1.0, rotations: dict[str, float] | None = None,
           nudges: dict[str, list[float]] | None = None) -> list[dict]:
    """旅程弧线：按旅程顺序从左到右排开，高潮场景位于弧线顶点（最远处），其余沿抛物线向镜头靠近。
    sizes 为 Blender 导出尺寸 [x, y, z]（米）。返回 [{"id", "at": [x, y, 0], "rot_z"}]。"""
    rotations, nudges = rotations or {}, nudges or {}
    xs, x = [], 0.0
    for i, sid in enumerate(order):
        w = float(sizes[sid][0])
        if i:
            x += float(sizes[order[i - 1]][0]) / 2 + gap * spacing + w / 2
        xs.append(x)
    mid = (xs[0] + xs[-1]) / 2
    xs = [v - mid for v in xs]
    span = max(1.0, (xs[-1] - xs[0]) / 2)
    apex = xs[order.index(climax)] if climax in order else 0.0
    depth = max(float(s[1]) for s in sizes.values()) * 0.6 * spacing
    out = []
    for sid, vx in zip(order, xs):
        t = (vx - apex) / span
        vy = depth * (1 - min(1.0, t * t))
        n = nudges.get(sid) or [0, 0]
        out.append({"id": sid, "at": [round(vx + n[0], 3), round(vy + n[1], 3), 0.0],
                    "rot_z": round(float(rotations.get(sid, 0.0)), 2)})
    return out


def apply_layout_ops(state: dict, ops: list[dict], ids: list[str]) -> tuple[dict, list[str]]:
    """整合只调位置、间距、朝向与相邻顺序；不改已验收的场景本身。"""
    st = copy.deepcopy(state)
    st.setdefault("spacing", 1.0)
    st.setdefault("rotations", {})
    st.setdefault("nudges", {})
    log = []
    for op in ops[:4]:
        try:
            kind = op.get("op")
            if kind == "spacing":
                st["spacing"] = round(max(0.6, min(1.6, st["spacing"] * float(op.get("factor", 1)))), 3)
            elif kind == "rotate" and op.get("scene") in ids:
                st["rotations"][op["scene"]] = round(max(-45, min(45, st["rotations"].get(op["scene"], 0) + float(op.get("deg", 0)))), 1)
            elif kind == "nudge" and op.get("scene") in ids:
                n = st["nudges"].get(op["scene"], [0, 0])
                st["nudges"][op["scene"]] = [round(max(-12, min(12, n[0] + float(op.get("dx", 0)))), 2),
                                             round(max(-12, min(12, n[1] + float(op.get("dy", 0)))), 2)]
            elif kind == "swap" and op.get("a") in ids and op.get("b") in ids:
                order = st["order"]
                a, b = order.index(op["a"]), order.index(op["b"])
                if abs(a - b) != 1:
                    log.append(f"跳过：只交换相邻场景（{op['a']} ↔ {op['b']}）")
                    continue
                order[a], order[b] = order[b], order[a]
            else:
                log.append(f"跳过：无效操作 {kind}")
                continue
            log.append(json.dumps(op, ensure_ascii=False))
        except (TypeError, ValueError) as e:
            log.append(f"跳过：{str(e)[:60]}")
    return st, log


def transform_points(points: list, at: list[float], rot_z: float) -> list:
    a = math.radians(rot_z)
    c, s = math.cos(a), math.sin(a)
    return [[round(p[0] * c - p[1] * s + at[0], 3), round(p[0] * s + p[1] * c + at[1], 3), round(p[2] + at[2], 3)]
            for p in points]


WORLD = """这是世界《{title}》整合后的样子：{form_text}（旅程顺序：{order}；高潮「{climax}」）。
后面依次附各场景已确认的目标图，供核对色彩与尺度。
确定性检查：{checks}
{objective}
按世界维度打分（0–10）：
{rubric}
整合只能调排布（星球上还能改相邻两站之间的地势连接），不能改场景本身。可用操作（最多 3 条，不需要就给空数组）：
- {{"op": "spacing", "factor": 0.7–1.4}}  整体间距{spacing_note}
- {{"op": "rotate", "scene": "场景id", "deg": ±5–30}}  让场景的出入口朝向相邻一站
- {{"op": "nudge", "scene": "场景id", "dx": ±1–8, "dy": ±1–8}}  单位米{nudge_note}
- {{"op": "swap", "a": "场景id", "b": "相邻场景id"}}  只在节奏明显更好时交换相邻两站
{link_ops}另外给每个场景一句“下次迭代该改什么”（这些意见只记录，不在本次整合中执行）。
只输出 JSON：{{"scores": {{"维度id": 分}}, "verdict": "一句导演结论", "layout_ops": [...], "scene_notes": {{"场景id": "一句"}}}}"""


LINK_OPS = ('- {"op": "link", "a": "场景id", "b": "相邻场景id", "kind": "terraces" | "ridge" | "isthmus", "width": 7–18, "saddle": ±1–2}'
            "  改一段地势连接：类型、宽度（米）、垭口高低（层）\n")

FORMS = {
    "planet": ("所有场景贴在同一颗小行星上，相邻两站之间由你规划的地势连接（阶梯台地 / 山脊垭口 / 低地地峡）融成一片；"
               "第一、二张是星球全景，之后是每一站转到镜头前的样子",
               "（改变星球半径：越大越平、间隔越宽）", "（沿东 / 北移动经纬度）"),
    "archipelago": ("按旅程从左到右排成群岛，高潮在弧线顶点", "", ""),
}


async def world_review(overview: list[str], targets: dict[str, str], *, bible: dict, order: list[str],
                       checks: list[dict], objective: str, direction: dict, model: str,
                       style_rules: str = "", on_delta=None, form: str = "archipelago") -> dict:
    dims = rubric(direction, "world_rubric")
    names = {s["id"]: s["name"] for s in bible["scenes"]}
    form_text, spacing_note, nudge_note = FORMS.get(form, FORMS["archipelago"])
    text = WORLD.format(title=bible["title"], order=" → ".join(names.get(i, i) for i in order),
                        form_text=form_text, spacing_note=spacing_note, nudge_note=nudge_note,
                        link_ops=LINK_OPS if form == "planet" else "",
                        climax=names.get(anchor_id(bible), ""),
                        checks="；".join(f"{c['name']}{'通过' if c['passed'] else '未通过'}（{c['detail']}）" for c in checks),
                        objective=objective, rubric=rubric_text(dims))
    groups = [("整合全景：" if form != "planet" else "星球全景与每一站：", overview)] + [(f"目标图 · {names.get(i, i)}：", [targets[i]]) for i in order if targets.get(i)]
    msg = vision._vision_msg(text, groups, 768)

    def ok(o):
        if not isinstance(o, dict) or clamp_scores(o.get("scores"), dims) is None:
            return "scores 需要全部世界维度的数值分：" + "、".join(x["id"] for x in dims)
        if not isinstance(o.get("layout_ops", []), list):
            return "layout_ops 需要数组"
        return None
    o = await LLM(model).json([{"role": "system", "content": persona(direction, style_rules)}, msg],
                              thinking_budget=2048, retries=2, validate=ok, on_delta=on_delta)
    scores = clamp_scores(o["scores"], dims)
    return {"scores": scores, "director": weighted(scores, dims), "verdict": str(o.get("verdict", ""))[:400],
            "layout_ops": [x for x in o.get("layout_ops") or [] if isinstance(x, dict)][:3],
            "scene_notes": {k: str(v)[:300] for k, v in (o.get("scene_notes") or {}).items() if k in names}}


# ───────────────────────────── 地势连接（星球） ─────────────────────────────

LINKS_PROMPT = """世界《{title}》的各站已经贴到同一颗小行星上。相邻两站之间不搭桥——由你用地势把它们连起来
（参考你的“大地势 / 复杂地势生成”：先定大地势，再让复杂地势在两站之间协调出来）。
下面是每一段连接两端的读数（岛缘高度换算成层，海面在第 {sea} 层；间隙为两站岛缘之间的海面宽度，米）：
{rows}
可选地势：
- terraces 阶梯台地：两端落差大时，台地一级级跌落 / 升起，通路做成台阶；
- ridge 山脊垭口：两端都高时，一条山脊连过去，中间压低成垭口，两侧山肩夹住通路；
- isthmus 低地地峡：两端都不高或想要开阔的喘息段时，一条贴海的低地，通路下到岸边再上去。
世界圣经里写的连接意图：{hints}
为每一段选地势、宽度（米，7–18；高潮前后可以宽一些、更有分量）、垭口偏移（只对 ridge：-2..2 层，负值更低），
写一句为什么（节奏、剪影、两端高度怎样协调）。
只输出 JSON：{{"links": [{{"id": "连接id", "kind": "terraces|ridge|isthmus", "width": 11, "saddle": 0, "why": "一句"}}]}}"""


def validate_link_plan(o: Any, ids: list[str]) -> str | None:
    if not isinstance(o, dict) or not isinstance(o.get("links"), list):
        return "需要 {\"links\": [...]}"
    got = {x.get("id") for x in o["links"] if isinstance(x, dict)}
    if not set(ids) <= got:
        return "缺少连接：" + "、".join(sorted(set(ids) - got))
    for x in o["links"]:
        if isinstance(x, dict) and x.get("id") in ids and x.get("kind") not in ("terraces", "ridge", "isthmus"):
            return f"{x.get('id')} 的 kind 必须是 terraces / ridge / isthmus"
    return None


def normalize_link_plan(o: dict, specs: list[dict]) -> dict[str, dict]:
    from . import planet as PL
    out = {}
    by = {x.get("id"): x for x in o.get("links") or [] if isinstance(x, dict)}
    for sp in specs:
        x, d = by.get(sp["id"]) or {}, PL.default_link(sp)
        kind = x.get("kind") if x.get("kind") in PL.LINK_KINDS else d["kind"]
        try:
            width = max(7.0, min(18.0, float(x.get("width", d["width"]))))
        except (TypeError, ValueError):
            width = d["width"]
        try:
            saddle = int(max(-2, min(2, round(float(x.get("saddle", 0) or 0)))))
        except (TypeError, ValueError):
            saddle = 0
        out[sp["id"]] = {"kind": kind, "width": round(width, 1), "saddle": saddle, "why": str(x.get("why", ""))[:200]}
    return out


def link_rows(specs: list[dict], names: dict[str, str]) -> str:
    from . import planet as PL
    rows = []
    for sp in specs:
        d = PL.default_link(sp)
        rows.append(f"- {sp['id']}：{names.get(sp['a'], sp['a'])} → {names.get(sp['b'], sp['b'])}；"
                    f"A 岛缘第 {sp['h_a']} 层、B 岛缘第 {sp['h_b']} 层，间隙 {sp['gap']:.0f} 米（规则建议 {PL.LINK_KINDS[d['kind']]}）")
    return "\n".join(rows)


async def plan_links(specs: list[dict], *, bible: dict, direction: dict, model: str, style_rules: str = "",
                     on_delta=None) -> dict[str, dict]:
    """Oskar 为每段相邻连接选地势（阶梯台地 / 山脊垭口 / 低地地峡）、宽度与垭口高低。"""
    if not specs:
        return {}
    names = {s["id"]: s["name"] for s in bible["scenes"]}
    hints = "；".join(f"{s['name']} → 下一站：{LINKS.get(s.get('link_to_next'), s.get('link_to_next'))}"
                     for s in bible["scenes"][:-1]) or "无"
    text = LINKS_PROMPT.format(title=bible["title"], sea=specs[0]["sea_level"], rows=link_rows(specs, names), hints=hints)
    ids = [sp["id"] for sp in specs]
    o = await LLM(model).json([{"role": "system", "content": persona(direction, style_rules)},
                               {"role": "user", "content": text}],
                              thinking_budget=1024, retries=2, validate=lambda x: validate_link_plan(x, ids), on_delta=on_delta)
    return normalize_link_plan(o, specs)
