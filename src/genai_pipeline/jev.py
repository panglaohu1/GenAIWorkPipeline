"""JEV System One 式的差异驱动迭代（本地实现，参考 TigerMessenger docs/JEV_PIPELINE.md）。

每轮：
  1. observe  —— 视觉模型对【目标 | 当前】并排图只观察一次，产出“差异台账”（逐条、可度量、带严重度），
                 绑定图片 SHA256；评分由台账确定性计算，不采用模型自由打分（避免“列了一堆差异还给 8 分”）。
  2. decide   —— 纯文本类型化决策（System One）：从台账 + 硬性检查 + 历史尝试中选择**一个**工作类别和 1–3 条焦点差异，
                 给出置信度与各类别概率。
  3. gate     —— 本地规则覆盖：必需的硬性检查失败 → 强制对应类别；置信度 < 阈值 → review（按严重度取最大差异）。
  4. 执行只针对焦点差异；下一轮 observe 验证焦点差异是否消除，退步则回滚并把失败尝试写入历史。
证据与决策保存在 runs/<id>/jev/，观察与图片哈希绑定，渲染变化后必须重新观察。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .llm import LLM, image_part

CATEGORIES = {
    "silhouette": "整体剪影/主体形状与目标不符（主体用错形状、轮廓不对）",
    "proportion": "部件之间的比例/尺寸关系不对（太大、太小、太长、太短）",
    "position": "部件位置/朝向/连接关系不对（错位、悬空、方向反了）",
    "missing": "目标中显眼的部件缺失",
    "color": "主色块/材质颜色与目标不符",
    "extra": "多出目标中没有的部件",
    "detail": "小特征（装饰、纹样、面部细节）不符，且大结构已基本正确",
}
SEVERITY_WEIGHT = {3: 2.0, 2: 1.0, 1: 0.4}
CONFIDENCE_GATE = 0.85

OBSERVE_SYSTEM = """你是严格的 3D 资产视觉验收员（只负责观察，不负责打分）。
输入是一张并排对比图：左侧【目标】概念图，右侧【当前】模型的实时渲染（3D 渲染与概念图的材质光影不同是正常的，不算差异）。
逐条列出当前与目标的差异，每条必须：
- 指明部件（用目标中的部件名，如“机头”“左主翼”“尾巴”）
- category 取 silhouette / proportion / position / missing / color / extra / detail 之一
- severity：3=一眼可见、破坏识别（主体形状错、关键部件缺失/错位、部件悬空与主体分离、主体碎成互不相连的块、整体尺寸差 30% 以上）；2=明显但不破坏识别；1=细节
- observation 写成可度量、可验证的描述（例：“主翼翼展约为机身长度的 0.6 倍，目标约 1.0 倍”），不要泛泛而谈
同时列出已经正确的方面（matches）。只根据图中实际可见内容，不要根据对象名称去想象目标应该长什么样。
如果【目标】图里根本看不到这个对象（目标图裁错了、被遮挡到无法辨认），把 target_visible 设为 false。
只输出 JSON：{"target_visible": true, "matches": ["..."], "diffs": [{"part": "", "category": "", "severity": 1, "observation": ""}]}"""

DECIDE_SYSTEM = """你是 System One 决策器：只根据提供的文本证据（观察台账、检查结果、历史尝试）选择下一轮的工作类别。
不看图片，不改资产。缺失的检查不等于通过。
类别及准则：
{criteria}
规则：一轮只选一个类别，优先处理严重度高、影响识别的问题；大结构（silhouette/proportion/position/missing）未解决前不要选 detail/color；
避开历史中已失败的同类改法，除非换一种具体做法。focus 选 1–3 条差异 id。
只输出 JSON：{{"choice": "类别", "confidence": 0-1, "probabilities": {{"类别": 概率, ...}}, "focus": ["d1", ...],
 "plan": "针对焦点差异的具体修改方法（部件、尺寸、位置的具体数值变化）", "target_gap": 0-1}}"""


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compose_compare(target: str, renders: list[str], out: Path, height: int = 512) -> Path:
    """左：目标；右：当前渲染（多视角横排）。并排给视觉模型，比分别给两组图更容易看出差异。"""
    def fit(p: str) -> Image.Image:
        im = Image.open(p).convert("RGB")
        return im.resize((int(im.width * height / im.height), height))
    ims = [fit(target)] + [fit(r) for r in renders]
    gap = 12
    W = sum(i.width for i in ims) + gap * (len(ims) - 1)
    sheet = Image.new("RGB", (W, height + 36), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for fp in (Path(__file__).resolve().parents[2] / "templates/godot_project/addons/pipeline/fonts/NotoSansCJKsc-Regular.otf",
               Path("/System/Library/Fonts/Hiragino Sans GB.ttc"), Path("/System/Library/Fonts/STHeiti Medium.ttc")):
        try:
            font = ImageFont.truetype(str(fp), 24)
            break
        except OSError:
            continue
    x = 0
    for i, im in enumerate(ims):
        sheet.paste(im, (x, 36))
        label = "目标" if i == 0 else f"当前 {Path(renders[i - 1]).stem.split('_')[-1]}"
        draw.text((x + 8, 4), label, fill=(200, 30, 30) if i == 0 else (30, 30, 30), font=font)
        x += im.width + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=90)
    return out


def ledger_score(diffs: list[dict]) -> float:
    """由差异台账确定性计分。复杂场景常列满 12 条，直接累加会饱和到 0、失去梯度
    （组装冠军已改用客观分）；前 4 条全额、之后递减，保留区分度。"""
    weights = sorted((SEVERITY_WEIGHT.get(int(d.get("severity", 1)), 0.4) for d in diffs), reverse=True)
    penalty = 0.0
    for i, w in enumerate(weights):
        penalty += w if i < 4 else w * (0.4 ** (i - 3))
    return round(max(0.0, 10.0 - min(penalty, 10.0)), 2)


def ledger_pass(diffs: list[dict], pass_score: float) -> bool:
    return ledger_score(diffs) >= pass_score and not any(int(d.get("severity", 1)) >= 2 for d in diffs)


async def observe(*, target: str, renders: list[str], subject: str, compare_path: Path, facts: str = "",
                  model_key: str = "main", on_delta=None, style_context: str = "") -> dict:
    compose_compare(target, renders, compare_path)
    msg = {"role": "user", "content": [
        {"type": "text", "text": f"对象：{subject}\n" + (f"客观数据：{facts}\n" if facts else "") + "请观察下图并输出差异台账 JSON。"},
        image_part(compare_path, max_side=1536)]}

    def check(o: Any) -> str | None:
        if not isinstance(o, dict) or not isinstance(o.get("diffs"), list):
            return "需要 {\"matches\": [...], \"diffs\": [...]}"
        for d in o["diffs"]:
            if d.get("category") not in CATEGORIES:
                return f"category 必须是 {list(CATEGORIES)} 之一"
            if int(d.get("severity", 0)) not in (1, 2, 3):
                return "severity 必须是 1/2/3"
        return None

    obs = await LLM(model_key).json([{"role": "system", "content": OBSERVE_SYSTEM + style_context}, msg], on_delta=on_delta,
                                    thinking_budget=4096, validate=check)
    diffs = sorted(obs["diffs"], key=lambda d: -int(d.get("severity", 1)))[:12]
    for i, d in enumerate(diffs):
        d["id"] = f"d{i + 1}"
        d["severity"] = int(d["severity"])
    return {"matches": obs.get("matches", [])[:8], "diffs": diffs, "score": ledger_score(diffs),
            "target_visible": obs.get("target_visible") is not False,
            "compare": str(compare_path), "render_sha256": [sha256(r) for r in renders],
            "target_sha256": sha256(target)}


def gate(decision: dict, observation: dict, checks: list[dict], conf_gate: float = CONFIDENCE_GATE) -> dict:
    """本地规则：硬性检查失败优先；置信度不足时按严重度取焦点。"""
    failed = [c for c in checks if c.get("required") and c.get("passed") is not True]
    diffs = observation["diffs"]
    ids = {d["id"] for d in diffs}
    focus = [f for f in decision.get("focus", []) if f in ids][:3]
    action = decision.get("choice") if decision.get("choice") in CATEGORIES else "review"
    conf = decision.get("confidence")
    reason = {"laya": "Laya System One 决策", "llm-review": "LLM 复核决策"}.get(decision.get("decider"), "System One 选择")
    if failed:
        action = failed[0].get("category", "position")
        reason = f"必需检查未通过：{failed[0]['name']}"
    elif not isinstance(conf, (int, float)) or conf < conf_gate:
        action = "review"
        reason = f"置信度 {conf} 低于 {conf_gate}，按严重度处理最大差异"
    if action == "review" or not focus:
        focus = [d["id"] for d in diffs[:2]]
    return {**decision, "next_step": action, "focus": focus, "gate_reason": reason,
            "hard_gate_blockers": failed, "auto_apply": True}


LAYA_CRITERIA = {
    "silhouette": "整体剪影/主体形状与目标不符",
    "proportion": "部件之间的比例或尺寸不对（太大、太小、太长、太短）",
    "position": "部件位置、朝向或连接不对，或有悬空部件",
    "missing": "目标中显眼的部件在当前模型中缺失",
    "color": "主色块或材质颜色与目标不符",
    "extra": "当前模型多出目标中没有的部件",
    "detail": "只剩小特征或装饰不符，大结构已基本正确",
}


async def _laya(state: dict) -> dict | None:
    """开源 System One（Laya）类型化决策；服务不可用时返回 None。"""
    import httpx

    from . import config
    from .services import manager
    if config.get("decision.backend", "laya") != "laya":
        return None
    text_state = {
        "任务": state["task"],
        "观察到的差异": [f"{o['id']} [{o['part']}] 类别={o['category']} 严重度={o['severity']}：{o['observation']}"
                     for o in state["observations"]],
        "已正确": state["matches"][:5],
        "检查": [f"{c['name']}：{'通过' if c['passed'] is True else ('未测' if c['passed'] is None else '未通过')}"
               for c in state["checks"]],
        "历史尝试": [f"第{h['round']}轮 {h['choice']} → {h['result']}（{h['score_before']}→{h['score_after']}）"
                 for h in state["history"]],
    }
    questions = {
        "next_step": {"type": "choice", "criteria": LAYA_CRITERIA, "instructions":
                      "根据观察到的差异、检查结果和历史尝试，下一轮最应该集中处理哪一类问题？"
                      "严重度高、影响识别的优先；未通过的必需检查优先；历史中已失败的类别除非换做法否则降低优先。"},
        "target_gap": {"type": "noul", "instructions": "观察是否仍然明确指出当前模型与目标之间的差异？"},
    }
    url = manager.services["decision"].url
    async def request():
        async with httpx.AsyncClient(timeout=60) as c:
            return await c.post(f"{url}/decide", json={"state": text_state, "questions": questions})
    r = await manager.run("decision", request, timeout=120)
    if r.status_code != 200:
        return None
    return r.json()


async def decide(*, subject: str, observation: dict, checks: list[dict], history: list[dict],
                 model_key: str = "main", on_delta=None) -> dict:
    from . import config
    state = {"task": f"让 3D 资产逼近目标概念图：{subject}",
             "observations": [{"id": d["id"], "part": d["part"], "category": d["category"],
                               "severity": d["severity"], "observation": d["observation"]} for d in observation["diffs"]],
             "matches": observation["matches"],
             "render_sha256": observation["render_sha256"],
             "checks": checks,
             "history": history[-6:]}
    gate_conf = float(config.get("decision.confidence_gate", CONFIDENCE_GATE))

    # 1) System One：开源 Laya 快速决策
    laya_res = None
    try:
        laya_res = await _laya(state)
    except Exception as e:  # 服务异常 → 交给 LLM 复核
        laya_res = {"error": str(e)[:300]}
    if laya_res and "answers" in laya_res:
        ans = laya_res["answers"]["next_step"]
        choice, conf = ans.get("choice"), ans.get("confidence")
        failed = [c for c in checks if c.get("required") and c.get("passed") is not True]
        if choice in CATEGORIES and isinstance(conf, (int, float)) and conf >= gate_conf and not failed:
            focus = [d["id"] for d in observation["diffs"] if d["category"] == choice][:3] or \
                    [d["id"] for d in observation["diffs"][:2]]
            dec = {"choice": choice, "confidence": conf, "probabilities": ans.get("probabilities"), "focus": focus,
                   "plan": "", "target_gap": laya_res["answers"].get("target_gap", {}).get("noul"),
                   "decider": "laya"}
            return {"evidence": state, "system_one": laya_res, **gate(dec, observation, checks, gate_conf)}

    # 2) 置信度不足 / 硬性检查失败 / 服务不可用 → 生成式模型复核（相当于 Jev 流程中的 Astra 复核）
    crit = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    msgs = [{"role": "system", "content": DECIDE_SYSTEM.format(criteria=crit)},
            {"role": "user", "content": json.dumps(
                {**state, "system_one": laya_res.get("answers") if laya_res else None}, ensure_ascii=False)}]

    def check(o: Any) -> str | None:
        if not isinstance(o, dict) or o.get("choice") not in CATEGORIES:
            return f"choice 必须是 {list(CATEGORIES)} 之一"
        return None

    try:
        dec = await LLM(model_key).json(msgs, on_delta=on_delta, thinking_budget=2048, validate=check, retries=1)
        dec["confidence"] = max(float(dec.get("confidence") or 0), gate_conf)  # 复核结论视为已审核
        dec["decider"] = "llm-review"
    except Exception as e:  # 复核也失败 → 按严重度
        dec = {"choice": "review", "confidence": 0.0, "error": str(e)[:300], "decider": "severity"}
    out = {"evidence": state, "system_one": laya_res, **gate(dec, observation, checks, gate_conf)}
    if laya_res and "answers" in laya_res:
        ns = laya_res["answers"]["next_step"]
        failed = [c["name"] for c in checks if c.get("required") and c.get("passed") is not True]
        why = (f"必需检查未通过（{'、'.join(failed)}）" if failed
               else f"置信度 {ns.get('confidence')} 未达 {gate_conf}")
        out["gate_reason"] = f"Laya 选 {ns.get('choice')}，{why}，由 LLM 复核：{out['gate_reason']}"
    return out


VERIFY_SYSTEM = """你是严格的 3D 资产验收员，做“成对比较”。图中从左到右：【目标】概念图、【A】上一版模型、【B】本轮修改后的模型（同一机位）。
逐条判断焦点差异在 B 中相对 A 是 improved（明显改善）/ same（基本没变）/ worse（更差），
再判断整体哪一版更接近目标（A / B / same），并列出 B 相对 A 新出现的明显问题。只根据图中实际可见内容。
只输出 JSON：{"focus": {"差异id": "improved|same|worse"}, "closer": "A|B|same", "new_problems": ["..."], "reason": "一句话"}"""


def compose_pair(target: str, prev: str, cur: str, out: Path, height: int = 512) -> Path:
    def fit(p: str) -> Image.Image:
        im = Image.open(p).convert("RGB")
        return im.resize((int(im.width * height / im.height), height))
    ims = [fit(target), fit(prev), fit(cur)]
    gap = 12
    sheet = Image.new("RGB", (sum(i.width for i in ims) + gap * 2, height + 36), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for fp in (Path(__file__).resolve().parents[2] / "templates/godot_project/addons/pipeline/fonts/NotoSansCJKsc-Regular.otf",
               Path("/System/Library/Fonts/Hiragino Sans GB.ttc")):
        try:
            font = ImageFont.truetype(str(fp), 24)
            break
        except OSError:
            continue
    x = 0
    for label, im in zip(("目标", "A 上一版", "B 本轮"), ims):
        sheet.paste(im, (x, 36))
        draw.text((x + 8, 4), label, fill=(200, 30, 30) if label == "目标" else (30, 30, 30), font=font)
        x += im.width + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=90)
    return out


async def verify_pair(*, target: str, prev_render: str, cur_render: str, focus: list[dict], out: Path,
                      model_key: str = "main", on_delta=None) -> dict:
    """成对比较：比两次独立打分稳定得多，用于决定保留本轮还是回滚。"""
    compose_pair(target, prev_render, cur_render, out)
    focus_txt = "\n".join(f"- {d['id']} [{d['part']}] {d['observation']}" for d in focus) or "- （无）"
    msg = {"role": "user", "content": [
        {"type": "text", "text": f"焦点差异（A 版本中存在的问题）：\n{focus_txt}\n请比较并输出 JSON。"},
        image_part(out, max_side=1536)]}

    def check(o: Any) -> str | None:
        if not isinstance(o, dict) or o.get("closer") not in ("A", "B", "same"):
            return "closer 必须是 A / B / same"
        return None

    try:
        res = await LLM(model_key).json([{"role": "system", "content": VERIFY_SYSTEM}, msg], on_delta=on_delta,
                                        thinking_budget=3072, validate=check, retries=1)
    except Exception as e:
        return {"closer": "same", "focus": {}, "new_problems": [], "error": str(e)[:200], "compare": str(out)}
    res["compare"] = str(out)
    return res


def accept_b(v: dict) -> bool:
    """B 被接受：整体更接近目标；或焦点有改善、没有变差且整体不比 A 差、没有新的明显问题。"""
    if v.get("closer") == "B":
        return True
    if v.get("closer") == "A":
        return False
    marks = list((v.get("focus") or {}).values())
    return "improved" in marks and "worse" not in marks and not v.get("new_problems")
