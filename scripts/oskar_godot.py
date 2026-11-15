#!/usr/bin/env python3
"""把一次 Oskar 构建的 GLB 接入 Godot 并核对（与流水线 oskar_godot 阶段共用 genai_pipeline.godot.oskar_scene）。

用法：PYTHONPATH=src .venv/bin/python scripts/oskar_godot.py RUN_DIR [--hero R.png --hero-left R.png] [--size X Y Z] [--target T.png]
RUN_DIR 下需有 blender/export/oskar_scene.glb；Godot 工程建在 RUN_DIR/godot_project。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genai_pipeline.godot import oskar_scene  # noqa: E402
from genai_pipeline.mcp_hub import MCPHub  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--hero")
    ap.add_argument("--hero-left")
    ap.add_argument("--size", nargs=3, type=float)
    ap.add_argument("--target")
    a = ap.parse_args()
    run = Path(a.run_dir).resolve()
    views = {k: v for k, v in (("hero", a.hero), ("hero_left", a.hero_left)) if v}
    async with MCPHub(run, godot_project=run / "godot_project", servers=["godot"]) as hub:
        res = await oskar_scene.setup_and_verify(hub, run / "godot_project", blender_views=views or None,
                                                 blender_size=a.size, target=a.target,
                                                 emit=lambda k, m, d=None: print(f"[{k}] {m}", file=sys.stderr))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
