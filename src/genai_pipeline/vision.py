"""视觉能力：生图客户端、候选图挑选、渲染/截图对目标图的验收评审。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from . import config
from .llm import LLM, image_part
from .services import manager

CONCEPT_SUFFIX = ("single isolated object, full view, three-quarter angle, centered, plain light grey studio "
                  "background, game asset concept art, clean readable silhouette, simple shapes, no text, no watermark")


async def _post(service: str, endpoint: str, body: dict, *, timeout: float, startup: float) -> httpx.Response:
    async def request():
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
            return await client.post(manager.services[service].url + endpoint, json=body)
    return await manager.run(service, request, timeout=startup)


async def generate_image(prompt: str, out: Path, *, backend: str | None = None, seed: int | None = None,
                         width: int | None = None, height: int | None = None, init_image: str | None = None,
                         strength: float | None = None) -> dict:
    body: dict[str, Any] = {"prompt": prompt, "out": str(out), "backend": backend or config.get("image.default_backend")}
    for k, v in (("seed", seed), ("width", width), ("height", height), ("init_image", init_image),
                 ("strength", strength)):
        if v is not None:
            body[k] = v
    r = await _post("image", "/generate", body, timeout=1800, startup=300)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(data.get("error", r.text))
    return data


async def generate_shape(image: str, out: Path, *, seed: int = 0) -> dict:
    """图生 3D：物体图（最好 RGBA 透明背景）→ 形体 GLB（无贴图）。"""
    c = config.get("shape3d", {})
    body = {"image": str(image), "out": str(out), "seed": int(seed), "steps": int(c.get("steps", 5)),
            "octree": int(c.get("octree", 256)), "guidance": float(c.get("guidance", 5.0)),
            "num_chunks": int(c.get("num_chunks", 20000)), "max_faces": int(c.get("max_faces", 20000))}
    r = await _post("shape3d", "/generate", body, timeout=1800, startup=600)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(data.get("error", r.text))
    return data


async def generate_audio(prompt: str, out: Path, *, seconds: float = 10, seed: int | None = None,
                         loop: bool = False) -> dict:
    """文字 → 环境音 / 音效 WAV（开源 MOSS-SoundEffect，本机 audio 服务）。loop=True 时首尾交叉淡化成可循环素材。"""
    body: dict[str, Any] = {"prompt": prompt, "out": str(out), "seconds": float(seconds), "loop": bool(loop)}
    if seed is not None:
        body["seed"] = int(seed)
    r = await _post("audio", "/generate", body, timeout=900, startup=300)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(data.get("error", r.text))
    return data


def _vision_msg(text: str, groups: list[tuple[str, list[str]]], max_side: int = 768) -> dict:
    parts: list[dict] = [{"type": "text", "text": text}]
    for label, paths in groups:
        parts.append({"type": "text", "text": label})
        parts += [image_part(p, max_side) for p in paths if Path(p).exists()]
    return {"role": "user", "content": parts}


async def choose_best(candidates: list[str], brief: str, model_key: str = "main", on_delta=None) -> dict:
    groups = [(f"候选 {i}:", [p]) for i, p in enumerate(candidates)]
    msg = _vision_msg(
        f"你是游戏美术总监。下面是同一资产的 {len(candidates)} 张概念图候选。资产要求：{brief}\n"
        "选出最适合作为 3D 建模目标图的一张：形体清晰、比例合理、单一物体、背景干净、能用简单几何体在 Blender 中还原。\n"
        '只输出 JSON：{"best": 编号, "reason": "中文一句话", "scores": [每张 0-10 的分数]}', groups, 512)
    return await LLM(model_key).json([msg], on_delta=on_delta, thinking_budget=2048,
                                     validate=lambda o: None if isinstance(o, dict) and isinstance(o.get("best"), int)
                                     and 0 <= o["best"] < len(candidates) else "best 必须是有效编号")


REVIEW_SCHEMA_HINT = ('{"score": 0-10 数字, "pass": 布尔, "matches": ["做得对的地方"], '
                      '"diffs": ["具体差异：哪个部件、差在哪（形状/比例/颜色/位置/缺失）"], '
                      '"next_actions": ["下一轮最该做的 1-4 个具体修改，按优先级"]}')


async def review_against_target(*, target: list[str], candidate: list[str], subject: str, criteria: list[str],
                                facts: str = "", pass_score: float = 7.5, model_key: str = "main",
                                on_delta=None, style_context: str = "") -> dict:
    crit = "\n".join(f"- {c}" for c in criteria) if criteria else "- 与目标图整体形体、比例、配色一致"
    msg = _vision_msg(
        f"你是严格的 3D 游戏资产验收员。对象：{subject}\n验收标准：\n{crit}\n{style_context}\n"
        + (f"客观检查数据：{facts}\n" if facts else "")
        + "请对比【目标图】和【当前结果】。只根据图片中实际可见的内容判断，不要臆测；"
          "3D 实时渲染不可能与概念图像素一致，重点看整体形体、比例、结构部件、主色调是否还原。\n"
          f"评分 0-10，≥{pass_score} 视为通过。只输出 JSON：{REVIEW_SCHEMA_HINT}",
        [("【目标图】", target), ("【当前结果】", candidate)])

    def check(o):
        if not isinstance(o, dict) or not isinstance(o.get("score"), (int, float)):
            return "需要数值 score"
        return None

    res = await LLM(model_key).json([msg], on_delta=on_delta, thinking_budget=3072, validate=check)
    res["score"] = float(res["score"])
    res["pass"] = res["score"] >= pass_score
    res.setdefault("diffs", [])
    res.setdefault("next_actions", [])
    return res


async def objective_similarity(target: str, renders: list[str], target_mask: str | None = None) -> dict | None:
    """客观评分：渲染图 vs 目标图的 SigLIP-2 / DINOv2 相似度。服务不可用时返回 None（流水线退回台账规则）。"""
    if not config.get("metrics.enabled", True):
        return None
    try:
        body: dict[str, Any] = {"target": str(target), "renders": [str(r) for r in renders]}
        if target_mask:
            body["target_mask"] = str(target_mask)
        r = await _post("metrics", "/similarity", body, timeout=300, startup=180)
        r.raise_for_status()
        d = r.json()
        return {"siglip": d["siglip"]["max"], "dino": d["dino"]["max"],
                "siglip_per": d["siglip"]["per"], "dino_per": d["dino"]["per"]}
    except Exception:  # 客观评分是增强项：失败不影响主流程
        return None


def objective_index(m: dict | None) -> float | None:
    """把两种相似度折算成同一刻度（单位 = 历史回放中的轮间波动），用于同一资产内比较。"""
    if not m:
        return None
    return m["dino"] / float(config.get("metrics.dino_unit", 0.025)) + \
        m["siglip"] / float(config.get("metrics.siglip_unit", 0.015))


async def segment_instances(image: str, prompts: list[str], out_dir: Path) -> dict | None:
    """SAM 3 实例分割：{prompt: [{box, score, area, mask}]}。服务不可用时返回 None（退回 VLM 框选）。"""
    if not config.get("sam.enabled", True):
        return None
    try:
        body = {"image": str(image), "prompts": prompts, "out_dir": str(out_dir),
                "threshold": float(config.get("sam.threshold", 0.3)),
                "max_instances": int(config.get("sam.max_instances", 30))}
        r = await _post("sam", "/segment", body, timeout=900, startup=300)
        r.raise_for_status()
        return r.json()["results"]
    except Exception:
        return None


async def text_match(images: list[str], texts: list[str]) -> list[list[float]] | None:
    """SigLIP-2 图文匹配概率 [图][文]；服务不可用时返回 None。"""
    try:
        r = await _post("metrics", "/text_match", {"images": [str(i) for i in images], "texts": texts},
                        timeout=300, startup=180)
        r.raise_for_status()
        return r.json()["probs"]
    except Exception:
        return None
