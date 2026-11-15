"""本管线自己的能力，按同一契约（meta / match / run / verify）登记。

以前它们是 stage_compose 里写死的一串步骤；现在组装阶段按「能力计划」逐个执行：match 说明这一步能不能做、为什么，
run 在当前场景上执行，verify 独立检查结果（不过关的结果不保留），每一步都记经验（data/experience/scene3d.jsonl）。

subject（组装上下文）：SimpleNamespace(ctx, hub, spec, rec, target, cam, roots)
"""
from __future__ import annotations

from pathlib import Path

from .contract import Capability, summarize_checks


def _P():
    from .. import pipeline as P   # 延迟导入：pipeline 也导入本模块
    return P


def _regs(s) -> dict:
    return ((s.spec.get("palette_measured") or {}).get("regions")) or {}


# ------------------------------------------------------------------ 天空盒
def _sky_match(s, p):
    if not s.cam or s.rec.get("env_done") or not s.ctx.opt("sky_dome", True):
        return []
    r = _regs(s)
    have = [k for k in ("sky", "water") if r.get(k)]
    return [{"id": "sky-dome", "score": len(have), "reason": f"目标图有{'、'.join({'sky': '天空', 'water': '水面'}[k] for k in have)}区域，"
             "可逐行量色、按相机换成仰角铺全景"}] if have else []


async def _sky_run(s, targets, p):
    await _P()._sky_dome(s.ctx, s.spec, s.rec, s.target, s.cam)
    return {"applied": ["sky-dome"] if s.rec.get("sky_dome") else [], "png": s.rec.get("sky_dome")}


def _sky_verify(s, res):
    png = res.get("png")
    ok = bool(png) and Path(png).exists()
    return summarize_checks([{"id": "file", "name": "全景图", "status": "pass" if ok else "fail",
                              "detail": png or "没有生成全景图（沿用纯色背景）"}])


# ------------------------------------------------------------------ 布光闭环
def _light_match(s, p):
    if not s.cam or s.rec.get("env_done") or not s.ctx.opt("light_fix", True):
        return []
    r = _regs(s)
    mask = r.get("terrain_rock") or r.get("terrain")
    return [{"id": "light-rig", "score": 1, "reason": "目标图有岩面区域，可用深度法线拟合亮暗比与环境色，再按它搭灯"}] if mask else []


async def _light_run(s, targets, p):
    await _P()._light_loop(s.ctx, s.hub, s.spec, s.rec, s.target, s.cam)
    return {"applied": ["light-rig"] if s.rec.get("light_rig") else [], "err": s.rec.get("light_err")}


def _light_skip(s):
    if s.cam and not s.rec.get("env_done") and s.ctx.opt("light_fix", True):
        return "目标图上没有可信的岩面区域（拆分自检判为圈错、或没拆出崖面），测不了亮暗关系，沿用日光预设"
    return ""


def _light_revert(s, res):
    s.rec.pop("light_rig", None)   # 误差太大的灯不保留：退回日光预设


def _light_verify(s, res):
    err = res.get("err")
    if err is None:
        return summarize_checks([{"id": "fit", "name": "布光残差", "status": "warn", "detail": "目标图测不出可靠的亮暗关系，沿用日光预设"}])
    thr = float(s.ctx.opt("light_fix_err", 0.08))
    st = "pass" if err <= thr else ("warn" if err <= 3 * thr else "fail")
    return summarize_checks([{"id": "fit", "name": "布光残差", "status": st,
                              "detail": f"亮暗比 / 主光色 / 环境色的对数误差 {err:.3f}（≤{thr:g} 合格，>{3 * thr:g} 不保留）"}])


# ------------------------------------------------------------------ 相机俯角
def _cam_match(s, p):
    if not s.cam or s.rec.get("rounds") or s.rec.get("camera_fit") is not None or not s.ctx.opt("camera_pitch_fit", True):
        return []
    return [{"id": "camera", "score": 1, "reason": "第一轮组装、还没比过俯角：在实测机位上下各试几档，按客观相似度选"}]


async def _cam_run(s, targets, p):
    P = _P()
    ctx, hub, spec, rec = s.ctx, s.hub, s.spec, s.rec
    rargs = P._rargs(spec, rec)
    r0 = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": "camfit_exposure", "camera": s.cam})
    c0 = await P._color_check(ctx, spec, s.target, (r0.get("images") or [""])[0]) if r0.get("images") else None
    if c0 and abs(c0.get("exposure_stops") or 0) >= 0.15:
        rec["exposure"] = round(max(-2.0, min(2.0, (rec.get("exposure") or 0.0) + c0["exposure_stops"])), 3)
        rargs["exposure"] = rec["exposure"]
    fitc = await P._fit_camera_pitch(ctx, hub, spec, s.target, rargs)
    rec["camera_fit"] = fitc or {}
    if fitc:
        s.cam = spec["camera"]
        ctx.emit("info", f"相机俯角闭环：试了 {fitc['tried']}（客观分），"
                         + (f"比实测再俯 {fitc['chosen_deg']}°（高出 {fitc['gain']}）" if fitc["chosen_deg"] else "实测俯角最好，不改"), fitc)
    return {"applied": ["camera"] if fitc else [], "fit": fitc}


def _cam_verify(s, res):
    fit = res.get("fit") or {}
    tried = fit.get("tried") or {}
    return summarize_checks([{"id": "tried", "name": "俯角比较", "status": "pass" if tried else "warn",
                              "detail": f"试了 {len(tried)} 档，选 {fit.get('chosen_deg', 0)}°" if tried else "没有可比的渲染"}])


# ------------------------------------------------------------------ 配色闭环
def _color_match(s, p):
    if not s.cam or s.rec.get("color_fix_done") or not s.ctx.opt("color_fix", True):
        return []
    n = len([k for k in _regs(s) if k.startswith("asset:")])
    return [{"id": "color-fix", "count": max(1, n), "score": n,
             "reason": f"{n} 个物件区域 + 地形 / 水 / 天有目标色，可按材质「设定色 ÷ 渲染色」逐步调基色"}]


async def _color_run(s, targets, p):
    await _P()._color_fix_loop(s.ctx, s.hub, s.spec, s.rec, s.target, s.cam, s.roots, _P()._rargs(s.spec, s.rec))
    return {"applied": ["color-fix"], "best": s.rec.get("color_fix_best")}


def _color_verify(s, res):
    b = res.get("best") or {}
    if not b:
        return summarize_checks([{"id": "de", "name": "平均色差", "status": "warn", "detail": "没有可比的渲染"}])
    de, frm = b.get("de"), b.get("from")
    st = "pass" if de is not None and frm is not None and de <= frm else "fail"
    return summarize_checks([{"id": "de", "name": "平均色差", "status": st, "detail": f"ΔE {frm} → {de}（{b.get('steps', 0)} 步保留）"}])


# ------------------------------------------------------------------ 拆分自检（检查型：只 match / verify）
def _audit_match(s, p):
    pm = s.spec.get("palette_measured") or {}
    cov = pm.get("coverage") or {}
    rows = []
    if cov.get("warn"):
        rows.append({"id": "coverage", "score": cov.get("unassigned", 0),
                     "reason": f"画面 {cov.get('unassigned', 0) * 100:.0f}% 的像素没归到任何物件 / 地形 / 水 / 天"})
    for k, why in (pm.get("suspect") or {}).items():
        rows.append({"id": k, "score": 1, "reason": why})
    return rows


NATIVE = [
    Capability({"id": "env.sky-dome", "tier": "scene3d", "layer": "environment", "origin": "native", "kind2": "measure",
                "name": "实测天空盒", "does": "目标图天空 / 水面逐行量色，按相机视场与俯角换成仰角，铺成 2:1 全景（PNG + HDR）；相机背景与远海都用它。",
                "sources": ["envgen.py measured_dome"], "targets": "有天空或水面区域的目标图",
                "keywords": ["天空", "天空盒", "海", "远景", "背景", "sky", "hdri"], "params": {}},
               _sky_match, _sky_run, _sky_verify),
    Capability({"id": "light.rig-fit", "tier": "scene3d", "layer": "lighting", "origin": "native", "kind2": "measure",
                "name": "布光闭环", "does": "目标图上用深度法线拟合「颜色 ≈ 环境 + 主光·max(0, 法线·光向)」，得到亮暗比与环境色；按它搭主光 / 补光 / 轮廓光 / 环境光，"
                "渲染图上用渲染法线拟合同一个量，误差降了才保留。",
                "sources": ["color_palette.py lambert_fit"], "targets": "有岩面区域、法线有变化的目标图",
                "keywords": ["布光", "光照", "灯光", "明暗", "阴影", "light", "lighting"],
                "skip_note": _light_skip,
                "params": {"steps": {"label": "最多几步", "type": "int", "min": 1, "max": 8, "default": 4}}},
               _light_match, _light_run, _light_verify, _light_revert),
    Capability({"id": "camera.pitch-fit", "tier": "scene3d", "layer": "camera", "origin": "native", "kind2": "measure",
                "name": "相机俯角闭环", "does": "先按实测机位补一次曝光，再在实测俯角上下各试几档，按与目标图的客观相似度选机位。",
                "sources": ["pipeline._fit_camera_pitch"], "targets": "第一轮组装",
                "keywords": ["相机", "机位", "俯角", "视角", "camera"], "params": {}},
               _cam_match, _cam_run, _cam_verify),
    Capability({"id": "color.fix", "tier": "scene3d", "layer": "material", "origin": "native", "kind2": "measure",
                "name": "配色闭环", "does": "同机位渲染 → 分区域色差验收 → 按材质「设定色 ÷ 渲染色」改基色（线性、逐通道、带阻尼）→ 再渲再验收；"
                "平均 ΔE 降、相似度不掉才保留。",
                "sources": ["pipeline._color_fix_loop"], "targets": "有目标色的物件 / 地形 / 水 / 天区域",
                "keywords": ["配色", "颜色", "色差", "调色", "color"],
                "params": {"steps": {"label": "最多几步", "type": "int", "min": 1, "max": 6, "default": 3}}},
               _color_match, _color_run, _color_verify),
    Capability({"id": "split.audit", "tier": "decompose", "layer": "split", "origin": "native", "kind2": "check",
                "name": "拆分自检", "does": "统计画面有多少像素归到了物件 / 地形 / 水 / 天；地形分区量出来的颜色和水面几乎一样时判为圈错。",
                "sources": ["color_palette.audit"], "targets": "拆分后的取色区域",
                "keywords": ["拆分", "覆盖", "漏拆", "圈错", "split"], "params": {}},
               _audit_match),
]

COMPOSE_PLAN = ("env.sky-dome", "light.rig-fit", "camera.pitch-fit", "color.fix")
