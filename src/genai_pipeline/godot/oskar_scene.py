"""Oskar 场景接入 Godot：确定性生成展示工程（不经 LLM），运行截图，并与 Blender 渲染做一致性核对。

- 场景：res://main.tscn → main.gd（extends addons/pipeline/oskar_diorama.gd）+ res://oskar_scene.json；
- 机位与光照参数取自 genai_pipeline/blender/look.py，与 Blender 渲染同源；
- 核对：导入无错、脚本与运行无错、截图非空、尺寸与 Blender 导出一致、同机位截图与 Blender 渲染的 DINO 相似度。
流水线阶段 oskar_godot 与脚本 scripts/oskar_godot.py 共用本模块。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from .. import vision
from ..blender import look

MAIN_GD = ('extends "res://addons/pipeline/oskar_diorama.gd"\n'
           "## Oskar 场景：展示、相机与光照逻辑在 oskar_diorama.gd；玩法写在这里（_ready 里先调用 super()）。\n")
SHOWCASE = [["hero", "day"], ["hero_left", "day"], ["hero", "dusk"]]
STEP = 2.0
MATCH_MIN_DINO = 0.80   # 同机位 Godot 截图 vs Blender 渲染的最低结构相似度（实测一致时约 0.90，见 docs/使用手册.md §16）
SIZE_TOL = 0.03


MAIN_GD_GAME = ('extends "res://addons/pipeline/oskar_diorama.gd"\n'
                "## 游戏：世界（地形 / 建筑 / 碰撞 / 光照）由 oskar_diorama.gd 搭好；在 _ready() 里先 super()，再放玩家与物件。\n"
                "const Kit = preload(\"res://addons/pipeline/kit.gd\")\n\n"
                "func _ready() -> void:\n\tsuper()\n")


def _spawn(walk: list, paths: list) -> list:
    """出生点：离第一条通路起点最近的平地可站立点（坡道格不在 walk 里）；没有通路时取最大台地的中心附近。"""
    if not walk:
        return paths[0][0] if paths and paths[0] else [0, 0, 0]
    if paths and paths[0]:
        s = paths[0][0]
        return min(walk, key=lambda p: (p[0] - s[0]) ** 2 + (p[2] - s[2]) ** 2 + 4 * (p[1] - s[1]) ** 2)
    zs = [round(p[1], 1) for p in walk]
    zc = max(set(zs), key=zs.count)                       # 最大的一层台地
    level = [p for p in walk if round(p[1], 1) == zc]
    cx = sum(p[0] for p in walk) / len(walk)
    cz = sum(p[2] for p in walk) / len(walk)
    return min(level, key=lambda p: (p[0] - cx) ** 2 + (p[2] - cz) ** 2)


def scene_config(glb_name: str = "oskar_scene.glb", walk: list | None = None, paths: list | None = None,
                 game: bool = False) -> dict:
    """walk / paths 为 Blender 坐标（Z 向上），这里换成 Godot 坐标。game=True：关掉轨道相机与展示巡游（相机交给玩家）。"""
    def unit(v):
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]
    gw = [look.to_godot(p) for p in walk or []]
    gp = [[look.to_godot(p) for p in path] for path in paths or []]
    return {"glb": f"res://assets/{glb_name}", "fov": round(look.camera_fov_deg(), 4), "frame_margin": look.FRAME_MARGIN,
            "views": {v: look.to_godot(unit(look.VIEW_DIRS[v])) for v in ("hero", "hero_left", "persp", "persp_left")},
            "lighting": {p: look.godot_lighting(p) for p in ("day", "dusk", "night")},
            # 流动的云（展示巡游截图时关闭，避免干扰与 Blender 渲染的同机位核对）与夜间柔光
            "clouds": {"count": 7, "speed": 1.0, "wind": [1.0, 0.0, 0.35], "in_showcase": False},
            "night": {"lanterns": 6, "patrol": True},
            "tour": SHOWCASE, "showcase": not game, "showcase_step": STEP, "orbit": not game,
            "ignore_bounds": ["oskar_sea"], "no_collision": ["oskar_sea", "oskar_props"], "collision": True,
            "walk": gw, "paths": gp, "spawn": _spawn(gw, gp)}


def _square_crop(src: str, dst: Path) -> str:
    from PIL import Image
    im = Image.open(src).convert("RGB")
    w, h = im.size
    s = min(w, h)
    im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).save(dst)
    return str(dst)


def _blank(path: str) -> bool:
    from PIL import Image, ImageStat
    return max(ImageStat.Stat(Image.open(path).convert("L")).stddev) < 4


async def setup_and_verify(hub, proj: Path, *, blender_views: dict[str, str] | None = None,
                           blender_size: list | None = None, target: str | None = None,
                           walk: list | None = None, paths: list | None = None,
                           emit: Callable[[str, str, Any], None] | None = None,
                           glb_name: str = "oskar_scene.glb", extra: dict | None = None,
                           main_gd: str | None = None) -> dict:
    """blender_views: {"hero": 渲染图, "hero_left": 渲染图}（日光）；blender_size: Blender 导出时的场景尺寸 [x, y, z]（Z 向上）。"""
    say = emit or (lambda kind, msg, data=None: None)
    say("info", "导入 GLB 到 Godot 工程…")
    imp = await hub.call("godot_import_assets", {})
    cfg = {**scene_config(glb_name, walk=walk, paths=paths), **(extra or {})}
    (proj / "oskar_scene.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    main = proj / "main.gd"
    template = Path(__file__).resolve().parents[3] / "templates" / "godot_project" / "main.gd"
    if main_gd:
        main.write_text(main_gd)   # 指定玩法脚本（如星球游戏）：总是写入
    elif not main.exists() or main.read_text() == template.read_text():
        main.write_text(MAIN_GD)   # 已写过玩法的 main.gd 保持不动
    say("info", "Godot 脚本检查与试运行截图…")
    chk = await hub.call("godot_check", {})
    shot_times = [round(STEP * i + STEP * 0.8, 2) for i in range(len(SHOWCASE))]
    for attempt in range(2):  # 偶发：窗口首次启动过慢没截到图，重跑一次
        run = await hub.call("godot_run", {"seconds": STEP * len(SHOWCASE) + 0.4, "shot_times": shot_times,
                                           "label": f"oskar_{time.strftime('%H%M%S')}"})
        shots = run.get("images", [])
        if len(shots) == len(SHOWCASE):
            break
        say("warn", f"Godot 试运行只截到 {len(shots)} 张图（退出码 {run.get('exit_code')}，耗时 {run.get('wall_seconds')}s），重试",
            {"errors": run.get("errors"), "log_tail": run.get("log_tail")})
    checks: list[dict] = []

    def add(name, passed, detail, required=True):
        checks.append({"name": name, "passed": bool(passed), "detail": detail, "required": required})

    add("Godot 导入", imp.get("ok") and not imp.get("errors"), "；".join(imp.get("errors", [])[:3]) or "导入成功")
    errs = sum(chk.get("script_errors", {}).values(), []) + chk.get("runtime_errors", []) + run.get("errors", [])
    add("脚本与运行无错误", not errs, "；".join(errs[:3]) or "无错误")
    blank = [Path(p).name for p in shots if _blank(p)]
    add("截图完整", len(shots) == len(SHOWCASE) and not blank,
        f"{len(shots)}/{len(SHOWCASE)} 张" + (f"，空白：{blank}" if blank else ""))
    assets = imp.get("assets") or {}
    info = assets.get(f"res://assets/{glb_name}") or next(iter(assets.values()), {})
    if blender_size and info.get("aabb_size"):
        g = info["aabb_size"]
        want = [blender_size[0], blender_size[2], blender_size[1]]  # Blender Z 向上 → Godot Y 向上
        err = max(abs(a - b) / max(b, 1e-3) for a, b in zip(g, want))
        add("尺寸与 Blender 一致", err <= SIZE_TOL, f"Godot {[round(x, 2) for x in g]} vs Blender {[round(x, 2) for x in want]}，"
                                                   f"最大偏差 {err:.1%}")
    match: dict[str, float] = {}
    crops: list[str] = []
    if shots and blender_views:
        crop_dir = Path(shots[0]).parent
        for (view, light), shot in zip(SHOWCASE, shots):
            crop = _square_crop(shot, crop_dir / f"crop_{view}_{light}.png")
            crops.append(crop)
            ref = blender_views.get(view)
            if light == "day" and ref:
                m = await vision.objective_similarity(ref, [crop])
                if m:
                    match[view] = round(m["dino"], 3)
        if match:
            add("与 Blender 渲染一致", min(match.values()) >= MATCH_MIN_DINO,
                "同机位 DINO " + "，".join(f"{k} {v:.3f}" for k, v in match.items()) + f"（下限 {MATCH_MIN_DINO}）")
        else:
            add("与 Blender 渲染一致", True, "相似度服务不可用，未核对", required=False)
    to_target = None
    if target and crops:
        m = await vision.objective_similarity(target, crops[:2])
        to_target = {k: round(m[k], 3) for k in ("dino", "siglip")} if m else None
    return {"ok": all(c["passed"] for c in checks if c["required"]), "checks": checks, "shots": shots, "crops": crops,
            "match": match, "to_target": to_target, "fps": (run.get("probe") or {}).get("fps"),
            "project": str(proj), "labels": [f"{'正面' if v == 'hero' else '左侧'}机位 · {'日光' if lt == 'day' else '黄昏'}"
                                             for v, lt in SHOWCASE]}
