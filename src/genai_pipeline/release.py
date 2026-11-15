"""游玩测试 → 下载与发布。

顺序：任务接入 Godot 后，先在本机进入游戏试玩；试玩确认后才提供下载与发布。
发布 = 生成一个可分发的版本目录 runs/<id>/release/：
  - 可玩工程包（Godot 工程 zip，任何装了 Godot 4 的机器都能打开运行）；
  - 本机安装了 Godot 导出模板时，另外导出 macOS 应用（.zip）与 Web 版本（index.html，可放到任何静态网站）；
    没有模板就如实说明缺什么（Godot 编辑器 → 编辑器 → 管理导出模板 → 下载）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config

PRESETS = """[preset.0]

name="macOS"
platform="macOS"
runnable=true
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter="*.json"
exclude_filter=""
export_path=""

[preset.0.options]

binary_format/architecture="universal"
application/bundle_identifier="com.genaistudio.{slug}"
application/name="{title}"
application/short_version="1.0"
application/version="1"
codesign/codesign=0
notarization/notarization=0

[preset.1]

name="Web"
platform="Web"
runnable=true
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter="*.json"
exclude_filter=""
export_path=""

[preset.1.options]

variant/thread_support=false
html/canvas_resize_policy=2
"""


def playable(run: dict, project: Path) -> bool:
    return run["mode"] in ("game", "assembly", "atlas", "oskar") and (project / "project.godot").is_file()


def confirm_playtest(store, run: dict, note: str = "") -> dict:
    opts = dict(run["options"])
    opts["playtest"] = {"confirmed": True, "at": time.time(), "note": str(note or "")[:500]}
    store.update_run(run["id"], options=opts)
    store.event(run["id"], None, "review", "人工试玩已确认：开放下载与发布", opts["playtest"])
    return opts["playtest"]


def _templates_dir(binary: str) -> tuple[Path | None, str]:
    try:
        v = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""
    m = re.match(r"(\d+\.\d+(?:\.\d+)?)\.(\w+)", v)
    if not m:
        return None, v
    ver = f"{m.group(1)}.{m.group(2)}"
    home = Path.home()
    for base in (home / "Library" / "Application Support" / "Godot" / "export_templates",
                 home / ".local" / "share" / "godot" / "export_templates"):
        if (base / ver).is_dir():
            return base / ver, v
    return None, v


def publish(store, run: dict, run_dir: Path, project: Path) -> dict:
    """在后台线程里执行；结果写进运行选项 release，并登记为产物。"""
    title = run["title"] or run["id"]
    slug = re.sub(r"[^a-z0-9]+", "", run["id"].lower())[-12:] or "game"
    out = run_dir / "release"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files, notes = [], []
    # 1. 可玩工程包
    tmp = out / "_project"
    shutil.copytree(project, tmp, ignore=shutil.ignore_patterns(".godot", "*.import"))
    (tmp / "README.txt").write_text(f"{title}\n\n用 Godot 4 打开本目录（导入项目 → 选择 project.godot），按 F5 运行。\n"
                                    "WASD 移动 · 空格跳 · 拖动鼠标转视角；星球游戏：每站找一封信，靠近电车按 F 上车，车上按 C 司机视角。\n")
    pkg = shutil.make_archive(str(out / f"{run['id']}-godot-project"), "zip", tmp)
    shutil.rmtree(tmp)
    files.append({"path": pkg, "label": "可玩工程包（Godot 4 打开即可运行）", "kind": "release"})
    # 2. 导出（需要本机 Godot 导出模板）
    binary = config.get("godot.binary")
    tdir, version = _templates_dir(binary)
    if tdir is None:
        notes.append(f"本机 Godot（{version or '未找到'}）没有安装导出模板，未导出 macOS 应用 / Web 版本。"
                     "在 Godot 编辑器 → 编辑器 → 管理导出模板 → 下载并安装后，再点一次「发布」即可。")
    else:
        (project / "export_presets.cfg").write_text(PRESETS.replace("{slug}", slug).replace("{title}", title.replace('"', "'")))
        if not (project / ".godot" / "imported").exists():
            subprocess.run([binary, "--headless", "--path", str(project), "--import"], timeout=900,
                           capture_output=True)
        for preset, target, label in (("macOS", out / f"{slug}-macos.zip", "macOS 应用（zip）"),
                                      ("Web", out / "web" / "index.html", "Web 版本（index.html，放到任意静态网站即可游玩）")):
            target.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([binary, "--headless", "--path", str(project), "--export-release", preset, str(target)],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode == 0 and target.exists():
                if preset == "Web":
                    target = Path(shutil.make_archive(str(out / f"{slug}-web"), "zip", target.parent))
                    label = "Web 版本（zip，解压后放到静态网站即可游玩）"
                files.append({"path": str(target), "label": label, "kind": "release"})
            else:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
                notes.append(f"{preset} 导出失败：{' / '.join(tail) or f'退出码 {r.returncode}'}")
    for f in files:
        store.add_artifact(run["id"], "deliver", "release", f["path"], f["label"])
    result = {"status": "done", "at": time.time(), "files": [f["label"] for f in files], "notes": notes}
    run = store.get_run(run["id"])
    opts = dict(run["options"])
    opts["release"] = result
    store.update_run(run["id"], options=opts)
    store.event(run["id"], None, "result", "发布完成：" + "；".join(result["files"]) + ("；" + "；".join(notes) if notes else ""),
                result)
    return result
