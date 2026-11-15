#!/usr/bin/env python3
"""Oskar 引擎试做：程序 → Blender 构建 → 多机位 × 日光/黄昏 渲染 + 灰阶对照 + 硬检查。
用法：GENAI_BLENDER_PORT=9899 PYTHONPATH=src .venv/bin/python scripts/oskar_trial.py projects/oskar-trials/programs/X.json [out_dir]
"""
import asyncio
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genai_pipeline.mcp_hub import MCPHub  # noqa: E402

VIEWS = ["hero", "hero_left", "front"]


async def run(prog_path: Path, out: Path, lightings=("day", "dusk")) -> dict:
    prog = json.loads(prog_path.read_text())
    out.mkdir(parents=True, exist_ok=True)
    async with MCPHub(out, servers=["blender"]) as hub:
        res = await hub.call("blender_build_oskar", {"program": prog, "palette": prog["palette"] if isinstance(prog.get("palette"), str) else ""})
        if not res.get("ok"):
            return res
        shots = {}
        for lt in lightings:
            r = await hub.call("blender_render", {"views": VIEWS, "label": f"{prog_path.stem}_{lt}", "lighting": lt})
            shots[lt] = r.get("images", [])
    sheet_rows = []
    for lt, imgs in shots.items():
        row = [Image.open(p).convert("RGB") for p in imgs]
        sheet_rows.append((lt, row))
        sheet_rows.append((lt + " 灰阶", [ImageOps.grayscale(i).convert("RGB") for i in row]))
    w = sum(i.width for i in sheet_rows[0][1])
    h = sum(r[1][0].height for r in sheet_rows)
    sheet = Image.new("RGB", (w, h), "white")
    y = 0
    for label, row in sheet_rows:
        x = 0
        for im in row:
            sheet.paste(im, (x, y))
            x += im.width
        ImageDraw.Draw(sheet).text((8, y + 6), label, fill=(255, 255, 255))
        y += row[0].height
    sheet_path = out / f"{prog_path.stem}_sheet.jpg"
    sheet.save(sheet_path, quality=88)
    res.update({"sheet": str(sheet_path), "shots": shots})
    (out / f"{prog_path.stem}_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


if __name__ == "__main__":
    p = Path(sys.argv[1])
    o = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("runs/oskar-trials") / p.stem
    r = asyncio.run(run(p, o.resolve()))
    print(json.dumps({k: v for k, v in r.items() if k != "shots"}, ensure_ascii=False, indent=1))
