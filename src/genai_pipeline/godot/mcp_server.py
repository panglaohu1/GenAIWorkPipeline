"""Godot MCP 服务端（stdio），基于 Godot 4.7 命令行。

  .venv/bin/python -m genai_pipeline.godot.mcp_server
环境变量:
  GENAI_RUN_DIR         运行目录（Blender 导出的 GLB 在 blender/export/ 下）
  GENAI_GODOT_PROJECT   Godot 工程目录（默认 $GENAI_RUN_DIR/godot_project，不存在则从模板创建）
所有文件读写限制在工程目录内。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .. import config

GODOT = config.get("godot.binary")
TEMPLATE = config.ROOT / "templates" / "godot_project"
RUN_DIR = Path(os.environ.get("GENAI_RUN_DIR", config.ROOT / "runs" / "_scratch")).resolve()
PROJECT = Path(os.environ.get("GENAI_GODOT_PROJECT", RUN_DIR / "godot_project")).resolve()
SHOTS = RUN_DIR / "godot_shots"
WRITABLE_EXT = {".gd", ".tscn", ".tres", ".gdshader", ".cfg", ".json", ".txt", ".md", ".godot"}
ERR_PAT = re.compile(r"(SCRIPT ERROR|Parse Error|ERROR:|Invalid call|Invalid access|Nonexistent function|"
                     r"Identifier .* not declared|Cannot call|Failed to load)", re.I)
# 退出时的资源泄漏提示等与游戏逻辑无关的引擎噪声
BENIGN = re.compile(r"(leaked at exit|RID allocations|ObjectDB instances leaked|resources still in use at exit)", re.I)
_run_counter = 0


_synced = False


def ensure_project() -> Path:
    global _synced
    if not (PROJECT / "project.godot").exists():
        PROJECT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(TEMPLATE, PROJECT, dirs_exist_ok=True)
    elif not _synced:  # 流水线自带的 addons/pipeline 始终与模板同步（工具箱升级后旧工程也能用）
        shutil.copytree(TEMPLATE / "addons" / "pipeline", PROJECT / "addons" / "pipeline", dirs_exist_ok=True)
    if not _synced:  # 新增的字体等资源需要导入后才能 load()
        _godot(["--headless", "--path", str(PROJECT), "--import"], timeout=300)
    _synced = True
    return PROJECT


def _inside(rel: str) -> Path:
    rel = rel.removeprefix("res://")
    p = (PROJECT / rel).resolve()
    if not p.is_relative_to(PROJECT):
        raise ValueError("路径必须在工程目录内")
    return p


def _godot(args: list[str], timeout: float = 120) -> tuple[int, str]:
    try:
        r = subprocess.run([GODOT, *args], capture_output=True, text=True, timeout=timeout, cwd=PROJECT)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -1, out + f"\n[pipeline] 超时 {timeout}s，进程已终止"


def _errors(log: str, limit: int = 30) -> list[str]:
    lines = log.splitlines()
    out = []
    for i, line in enumerate(lines):
        if ERR_PAT.search(line) and "pipeline" not in line.lower() and not BENIGN.search(line):
            ctx = " | ".join(x.strip() for x in lines[i:i + 3] if x.strip())
            out.append(re.sub(r"\x1b\[[0-9;]*m", "", ctx)[:400])
        if len(out) >= limit:
            break
    return out


mcp = MCPServer(
    "godot",
    instructions=(
        "操作一个 Godot 4.7 工程。主场景 res://main.tscn 挂 res://main.gd，推荐完全用 GDScript 代码搭建世界；"
        "资产 GLB 在 res://assets/。输入动作已注册: move_left/right/forward/back, jump, action, sprint。"
        "玩家节点请 add_to_group(\"player\") 以便测试记录轨迹。改完脚本先 godot_check 再 godot_run。"
    ),
)


@mcp.tool()
def godot_project_files() -> str:
    """列出工程文件（不含 .godot/ 缓存与 .import 文件）。"""
    ensure_project()
    files = []
    for p in sorted(PROJECT.rglob("*")):
        rel = p.relative_to(PROJECT)
        if p.is_file() and not rel.parts[0].startswith(".") and p.suffix != ".import":
            files.append({"path": "res://" + str(rel), "bytes": p.stat().st_size})
    return json.dumps({"project": str(PROJECT), "files": files}, ensure_ascii=False)


@mcp.tool()
def godot_read_file(path: str) -> str:
    """读取工程内文本文件（res:// 路径或相对路径）。"""
    ensure_project()
    p = _inside(path)
    if not p.is_file():
        return json.dumps({"ok": False, "error": f"文件不存在: {path}（先用 godot_project_files 查看有哪些文件）"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "path": path, "content": p.read_text()[:60000]}, ensure_ascii=False)


@mcp.tool()
def godot_write_file(path: str, content: str) -> str:
    """写入/覆盖工程内文本文件（.gd .tscn .tres .gdshader .cfg .json .txt .md）。GDScript 用 Tab 缩进。"""
    ensure_project()
    p = _inside(path)
    if p.suffix not in WRITABLE_EXT:
        return json.dumps({"ok": False, "error": f"不允许写入 {p.suffix} 文件"}, ensure_ascii=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return json.dumps({"ok": True, "path": "res://" + str(p.relative_to(PROJECT)), "bytes": len(content)},
                      ensure_ascii=False)


@mcp.tool()
def godot_import_assets() -> str:
    """把 Blender 导出的 GLB 复制到 res://assets/ 并执行 Godot 导入；返回每个资产的尺寸（米，Y 向上）。"""
    ensure_project()
    src = RUN_DIR / "blender" / "export"
    copied = []
    for glb in sorted(src.glob("*.glb")) if src.exists() else []:
        dst = PROJECT / "assets" / glb.name
        if not dst.exists() or dst.read_bytes() != glb.read_bytes():
            shutil.copy2(glb, dst)
        copied.append("res://assets/" + glb.name)
    code, log = _godot(["--headless", "--path", str(PROJECT), "--import"], timeout=300)
    assets = sorted("res://assets/" + p.name for p in (PROJECT / "assets").glob("*.glb"))
    inspect = {}
    if assets:
        _, ilog = _godot(["--headless", "--path", str(PROJECT), "--script", "res://addons/pipeline/inspect.gd",
                          "--", *assets])
        m = re.search(r"PIPELINE_INSPECT (\{.*\})", ilog)
        if m:
            inspect = json.loads(m.group(1))
    return json.dumps({"ok": code == 0, "copied": copied, "assets": inspect, "errors": _errors(log)},
                      ensure_ascii=False)


@mcp.tool()
def godot_check() -> str:
    """静态检查所有 .gd 脚本语法，并无头启动主场景几帧捕获运行时错误。"""
    ensure_project()
    results = {}
    for gd in sorted(PROJECT.rglob("*.gd")):
        rel = gd.relative_to(PROJECT)
        if rel.parts[0].startswith(".") or rel.parts[:2] == ("addons", "pipeline"):
            continue
        _, log = _godot(["--headless", "--path", str(PROJECT), "--check-only", "--script", f"res://{rel}"],
                        timeout=60)
        errs = _errors(log)
        if errs:
            results[f"res://{rel}"] = errs
    code, log = _godot(["--headless", "--path", str(PROJECT), "--quit-after", "120"], timeout=120)
    runtime = _errors(log)
    ok = not results and not runtime
    return json.dumps({"ok": ok, "script_errors": results, "runtime_errors": runtime}, ensure_ascii=False)


@mcp.tool()
def godot_run(seconds: float = 6.0, inputs: list[dict] | None = None, shot_times: list[float] | None = None,
              label: str = "") -> str:
    """打开窗口运行游戏若干秒并截图，返回错误、游戏 print 输出（game_output）、FPS、player 组节点轨迹和截图路径（images）。

    inputs: 模拟输入，如 [{"action":"move_forward","start":1.0,"duration":1.5},{"action":"jump","start":2.0,"duration":0.2}]
    shot_times: 截图时间点（秒），默认结束前 0.5 秒截一张。
    """
    global _run_counter
    ensure_project()
    _run_counter += 1
    shot_dir = SHOTS / f"run{_run_counter:03d}{('_' + re.sub(r'[^A-Za-z0-9_-]', '_', label)) if label else ''}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    w, h = config.get("godot.screenshot_size", [1280, 720])
    # 置顶窗口：被遮挡 / 在后台时 macOS 可能暂停渲染，试运行会卡住直到超时（实测偶发）
    args = ["--path", str(PROJECT), "--resolution", f"{w}x{h}", "--always-on-top", "--",
            f"--pipeline-seconds={seconds}", f"--pipeline-shots={shot_dir}"]
    if shot_times:
        args.append("--pipeline-shot-times=" + ",".join(str(t) for t in sorted(shot_times)))
    if inputs:
        args.append("--pipeline-inputs=" + json.dumps(inputs))
    t0 = time.time()
    code, log = _godot(args, timeout=seconds + 60)
    probe = {}
    m = re.search(r"PIPELINE_PROBE (\{.*\})", log)
    if m:
        probe = json.loads(m.group(1))
    images = sorted(str(p) for p in shot_dir.glob("*.png"))
    track = probe.pop("player_track", [])
    summary_track = track[:: max(1, len(track) // 12)] if track else []
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log).splitlines()
    game_output = [ln for ln in clean if ln.strip() and not ln.startswith(("PIPELINE_PROBE", "Godot Engine v"))
                   and "Vulkan" not in ln and "Metal" not in ln][-40:]
    return json.dumps({
        "ok": code == 0 and bool(m) and not _errors(log),
        "game_output": game_output,
        "exit_code": code, "wall_seconds": round(time.time() - t0, 1), "errors": _errors(log),
        "probe": probe, "player_track_sample": summary_track, "images": images,
        "log_tail": re.sub(r"\x1b\[[0-9;]*m", "", log)[-1500:] if not m else "",
    }, ensure_ascii=False)


if __name__ == "__main__":
    ensure_project()
    mcp.run("stdio")
