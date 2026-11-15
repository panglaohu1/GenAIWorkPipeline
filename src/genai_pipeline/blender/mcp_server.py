"""Blender MCP 服务端（stdio）。

由流水线为每次运行启动；也可以注册到任何 MCP 客户端（Claude Desktop / Codex 等）：
  .venv/bin/python -m genai_pipeline.blender.mcp_server
环境变量:
  GENAI_RUN_DIR   输出根目录（渲染、.blend、.glb 都写在其下的 blender/ 中）
工作进程: 若端口上没有 Blender worker，会自动启动（headless 由 config blender.headless 决定）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .. import config

WORKER = Path(__file__).with_name("worker.py")
PORT = int(os.environ.get("GENAI_BLENDER_PORT") or config.get("blender.worker_port", 9877))
RUN_DIR = Path(os.environ.get("GENAI_RUN_DIR", config.ROOT / "runs" / "_scratch")).resolve()
OUT = RUN_DIR / "blender"
EXEC_TIMEOUT = 300

_proc: subprocess.Popen | None = None
_render_counter = 0


def _connect(timeout: float) -> socket.socket:
    return socket.create_connection(("127.0.0.1", PORT), timeout=timeout)


def _start_worker() -> None:
    global _proc
    cmd = [config.get("blender.binary"), "--factory-startup"]
    if config.get("blender.headless", True):
        cmd.insert(1, "-b")
    cmd += ["--python", str(WORKER), "--", "--port", str(PORT), "--workspace", str(RUN_DIR)]
    log = open(config.ROOT / "logs" / "blender-worker.log", "ab")
    _proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            _connect(1).close()
            return
        except OSError:
            if _proc.poll() is not None:
                raise RuntimeError("Blender worker 启动失败，见 logs/blender-worker.log")
            time.sleep(0.5)
    raise RuntimeError("Blender worker 启动超时")


def _restart_worker() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.kill()
    _proc = None
    # 端口上可能是外部启动的 worker：尝试让它退出
    try:
        s = _connect(2)
        s.sendall(b'{"id":0,"op":"quit"}\n')
        s.close()
    except OSError:
        pass
    time.sleep(1)
    _start_worker()


def call(op: str, timeout: float = EXEC_TIMEOUT, **args) -> dict:
    for attempt in (1, 2):
        try:
            s = _connect(3)
        except OSError:
            _start_worker()
            s = _connect(3)
        s.settimeout(timeout)
        f = s.makefile("rwb")
        try:
            f.write((json.dumps({"id": 1, "op": op, "args": args}, ensure_ascii=False) + "\n").encode())
            f.flush()
            line = f.readline()
            if not line:
                raise ConnectionError("worker closed connection")
            return json.loads(line)
        except socket.timeout:
            _restart_worker()
            return {"ok": False, "error": f"操作超时（>{timeout}s），Blender 已重启，场景已丢失。请避免死循环或过重的操作。"}
        except (ConnectionError, OSError):
            if attempt == 2:
                raise
            _restart_worker()
        finally:
            s.close()
    return {"ok": False, "error": "unreachable"}


def _ensure_current_worker() -> None:
    """worker.py 更新后，在新运行开始时重启常驻的 Blender worker（此时没有需要保留的场景）。"""
    import hashlib
    h = hashlib.sha1(WORKER.read_bytes())
    for f in [WORKER.parent / "look.py"] + sorted((WORKER.parent.parent / "oskar").glob("*.py")):  # 与 worker._code_hash 一致
        h.update(f.read_bytes())
    want = h.hexdigest()[:12]
    try:
        r = call("ping", timeout=10)
    except OSError:
        return
    if (r.get("result") or {}).get("code") != want:
        _restart_worker()


def _out(resp: dict, **extra) -> str:
    if resp.get("ok"):
        body = {"ok": True, **(resp.get("result") or {}), **extra}
    else:
        body = {"ok": False, "error": resp.get("error"), "traceback": resp.get("traceback"), **extra}
    return json.dumps(body, ensure_ascii=False)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or "asset"


mcp = MCPServer(
    "blender",
    instructions=(
        "控制一个 Blender 5.2 实例建模。单位米，Z 轴向上，模型底部应位于 z=0。"
        "主要用 blender_run_python 编写 bpy 代码，命名空间里有 H 建模助手。"
        "每做完一批修改就调用 blender_render 看效果。"
    ),
)


@mcp.tool()
def blender_scene_info() -> str:
    """返回场景对象列表、三角面数、包围盒尺寸（米）、材质、是否有非有限变换。"""
    return _out(call("scene_info"))


@mcp.tool()
def blender_reset_scene() -> str:
    """清空场景（删除所有对象、孤立网格和材质）。开始一个新资产前调用。"""
    return _out(call("reset"))


@mcp.tool()
def blender_run_python(code: str) -> str:
    """在 Blender 中执行 Python 代码（持久命名空间，可跨调用复用变量）。

    可用名字: bpy, bmesh, math, Vector, Matrix, Euler, H。给变量 result 赋值可返回数据；print 输出会返回。
    H 建模助手（单位米、角度为度，都返回对象，均支持 name/location/rotation/material/parent 参数）:
      H.mat(name, color='#rrggbb'|(r,g,b), metallic=0, roughness=0.5, emission=None, emission_strength=0, alpha=1.0)  alpha<1 为透明（玻璃罩）
      H.box(name, size=(x,y,z), bevel=0)      H.cylinder(name, radius, depth, vertices=24)
      H.cone(name, radius1, radius2, depth)    H.sphere(name, radius, scale=(1,1,1))
      H.torus(name, major, minor)              H.plane(name, size)
      H.lathe(name, [(r,z),...])  回转体        H.extrude_profile(name, [(x,z),...], depth)  轮廓挤出
      H.limb(name, start=(x,y,z), end=(x,y,z), radius, radius_end=None)  两点间的肢体/尾巴/脖子（自动朝向，末端可变细）
      H.capsule(name, radius, length, axis='Z')  胶囊体（圆润躯干/四肢）   H.rounded_box(name, size, radius=0.08)  圆角盒
      H.mesh(name, verts, faces)  自定义网格
      H.snap(obj, to, side='+X'|'-X'|'+Y'|'-Y'|'+Z'|'-Z', overlap=0.02)  把部件贴到另一部件的某一侧并居中对齐（组装部件首选，避免手算坐标）
      H.bounds(obj) -> {'min','max','size','center'}  查询部件世界包围盒
      H.bevel(o,w) H.subsurf(o,l) H.mirror(o,'X') H.array(o,n,offset) H.smooth(o)
      H.join([objs], name) H.apply_modifiers() H.empty(name) H.get(name) H.delete(name)
      H.ground_to_origin()  整体落地并居中   H.clear()
    一次调用只做一个部件或一组相关部件，出错时根据报错修改后重试。
    """
    return _out(call("exec", code=code))


@mcp.tool()
def blender_render(views: list[str] | None = None, label: str = "", lighting: str = "", camera: dict | None = None,
                   backdrop: list[float] | None = None, sun_dir: list[float] | None = None,
                   exposure: float = 0.0, sky: str = "", rig: dict | None = None) -> str:
    """渲染当前场景用于检查。views 可选: front, back, side, left, top, persp, persp_back；
    给了 camera {"eye","target","fov_deg","aspect"} 时可用 "ref"（参考图机位）；backdrop 为背景色 [r,g,b] 0–1。
    默认 ["front", "side", "persp"]。返回图片路径（images），调用方会把图片给你看。"""
    global _render_counter
    _render_counter += 1
    existing = [int(m.group(1)) for f in (OUT / "renders").glob("r*_*.png")
                if (m := re.match(r"r(\d+)_", f.name))] if (OUT / "renders").exists() else []
    _render_counter = max(_render_counter, max(existing, default=0) + 1)  # 续跑时不覆盖旧渲染
    views = views or ["front", "side", "persp"]
    prefix = f"r{_render_counter:03d}" + (f"_{_safe_name(label)}" if label else "")
    resp = call("render", views=views, out_dir=str(OUT / "renders"), prefix=prefix,
                size=int(config.get("blender.render_size", 768)),
                engine=config.get("blender.render_engine", "BLENDER_EEVEE"), lighting=lighting or None,
                camera=camera or None, backdrop=backdrop or None, sun_dir=sun_dir or None,
                exposure=exposure or None, sky=sky or None, rig=rig or None)
    images = (resp.get("result") or {}).get("paths", []) if resp.get("ok") else []
    return _out(resp, images=images)


@mcp.tool()
def blender_build_blueprint(blueprint: dict) -> str:
    """按结构化部件蓝图重建整个场景（确定性构建，不需要写代码）。

    blueprint = {"parts": [部件, ...]}，部件按依赖顺序排列（attach 引用的部件必须在前面）。部件字段：
      name, shape: box|rounded_box|sphere|capsule|cylinder|cone|limb|torus|dome|rock, color: '#rrggbb', roughness, metallic, alpha
      尺寸: box/rounded_box/sphere → size:[x,y,z]（sphere 为椭球直径）；capsule → radius,length,axis('X'|'Y'|'Z')；
            cylinder → radius,depth,axis；cone → radius1,radius2,depth,axis；limb → from:[x,y,z], to:[x,y,z], radius, radius_end；
            torus → major,minor,axis
      位置（二选一）: at:[x,y,z] 部件中心；attach:{"to": 已有部件名, "side": "+X|-X|+Y|-Y|+Z|-Z", "overlap": 0.02, "offset":[x,y,z]}
      array:{"count": n, "along": "X"} 沿父部件全长均匀排布，或 {"count": n, "offset": [dx,dy,dz]} 等距重复；dome → size:[x,y,z] 半球；rock → size:[x,y,z] 不规则岩块
      rotation:[x,y,z] 度；mirror:"X" 在 x 取反位置生成左右对称副本（"Y" 为前后对称）
    返回每个部件的尺寸/中心、构建错误、悬空部件列表。"""
    return _out(call("build_blueprint", blueprint=blueprint))


@mcp.tool()
def blender_build_oskar(program: dict, palette: str = "") -> str:
    """Oskar 式引擎：按场景程序（不规则网格 + 角点状态 + 模块 + WFC）重建整个场景。
    program = {"seed", "grid": {"radius", "cell"}, "level_h", "sea_level", "items": [stack|mass|building|wall|path|clear]}
    返回硬检查（悬空 / 台面层次 / 通路净空）、WFC 瓦片统计、面数。palette: sea_stack | citadel。"""
    plan = RUN_DIR / "capabilities" / "plan.json"
    if isinstance(program, dict) and program.get("items") and "capabilities" not in program and plan.exists():
        # 体素层能力（REACode 能力库：石拱、悬垂、海中石柱、隧道、灯塔…）：管线写的计划，建状态后、出网格前执行
        try:
            program = {**program, "capabilities": json.loads(plan.read_text()).get("voxel")}
        except (OSError, ValueError):
            pass
    return _out(call("build_oskar", program=program, palette=palette or None))


@mcp.tool()
def blender_compose_scene(items: list[dict]) -> str:
    """按布局把多个子资产 GLB 组装成一个场景（会先清空场景）。
    items=[{"id": 实例名, "glb": GLB 路径, "at": [x,y,z] 底部中心位置（米）, "rot_z": 绕 Z 旋转（度）, "scale": 等比缩放}]
    返回每个实例的包围盒、总尺寸与面数。"""
    return _out(call("compose_scene", items=items))


@mcp.tool()
def blender_material_stats(image: str, camera: dict, target: str = "", target_mask: str = "") -> str:
    """配色闭环的测量：按 image（同机位 ref 渲染图）统计每个实例的每种材质在画面上的像素中位色，与其设定色对照。
    返回 materials=[{root, material, px, rendered, authored}]（sRGB 0–1）。"""
    return _out(call("material_stats", image=image, camera=camera, target=target or None,
                     target_mask=target_mask or None))


@mcp.tool()
def blender_recolor(fixes: dict, roots: dict) -> str:
    """按系数改材质基色：fixes={资产 id: {材质名: [r,g,b] 线性乘数}}，roots={实例名: 资产 id}。"""
    return _out(call("recolor", fixes=fixes, roots=roots))


@mcp.tool()
def blender_normal_pass(camera: dict, size: list[int], label: str = "normal") -> str:
    """同机位法线通道图（相机坐标 OpenCV，n·0.5+0.5，透明处无几何），供布光闭环拟合亮暗比。返回 path。"""
    out = OUT / "renders" / f"{_safe_name(label)}_normal.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    return _out(call("normal_pass", camera=camera, size=size, out=str(out)))


@mcp.tool()
def blender_compose_planet(items: list[dict], radius: float = 0.0, gap: float = 10.0, links: list | None = None,
                           tilt: float = 18.0) -> str:
    """把多个场景 GLB 贴到同一颗小行星上（会先清空场景）。items=[{"id","glb","lat","lon","yaw","r"}]，
    缺经纬度或 radius=0 时按旅程带自动排布；去掉各场景的方形海盘，换成整颗海洋球，links 非空时才沿大圆生成旧版陆桥；
    atlas 整合传 links=[]，并把 Oskar 引擎生成的地势连接作为 kind="link" 的 items 一起贴上（与场景同一投影）。
    返回半径、每个场景的经纬度、尺寸与面数。"""
    return _out(call("compose_planet", items=items, radius=radius, gap=gap, links=links, tilt=tilt))


@mcp.tool()
def blender_oskar_look(outline: float = 0.0) -> str:
    """给当前场景的网格加 Oskar 统一外观：平面着色 + 反转外壳描边（outline 为厚度米，0 = 按尺寸自适应）。导出前调用。"""
    return _out(call("oskar_look", outline=outline))


@mcp.tool()
def blender_export_glb(asset_name: str) -> str:
    """把当前场景导出为游戏资产 GLB（自动应用修改器、Y 轴向上），并做完整性检查。"""
    path = OUT / "export" / f"{_safe_name(asset_name)}.glb"
    return _out(call("export_glb", path=str(path)))


@mcp.tool()
def blender_save_blend(name: str) -> str:
    """把当前场景另存为 .blend（保存副本，不改变当前打开文件）。"""
    path = OUT / "blend" / f"{_safe_name(name)}.blend"
    return _out(call("save_blend", path=str(path)))


@mcp.tool()
def blender_open_blend(path: str) -> str:
    """打开一个已有 .blend 文件（例如要迭代的原始资产副本）。"""
    return _out(call("open_blend", path=str(Path(path).expanduser().resolve())))


@mcp.tool()
def blender_refine_generated(path: str, size_m: list[float], budget: int = 8000, image: str = "", mask: str = "",
                             colors: int = 6, name: str = "asset", palette: list[str] | None = None,
                             bands: list[dict] | None = None) -> str:
    """整理图生 3D 的形体网格（会先清空场景）：合并 → 按目标剪影自动选正面朝向（朝 -Y）→ 缩放到 size_m →
    精简到 budget 三角面 → 从目标图正面投影取色并量化成 colors 个色块（逐面平涂）→ 落地居中。
    image 为该资产的目标图（裁剪），mask 为同尺寸掩码（白 = 物体）。返回朝向、剪影 IoU、面数、尺寸、色块。"""
    return _out(call("refine_generated", path=str(Path(path).expanduser().resolve()), size_m=size_m, budget=budget,
                     image=image or None, mask=mask or None, colors=colors, name=_safe_name(name),
                     palette=palette or None, bands=bands or None))


@mcp.tool()
def blender_fit_to_size(size_m: list[float]) -> str:
    """把当前场景所有网格等比缩放到 size_m（水平最大边对齐），落地居中。"""
    return _out(call("fit_to_size", size_m=size_m))


@mcp.tool()
def blender_build_backdrop(size_m: list[float], image: str = "", mask: str = "", name: str = "backdrop",
                           seed: int = 0, colors: int = 4, flat: bool = False) -> str:
    """远景层（云海、海面、远处地平线）：会先清空场景，在 size_m 范围铺一片起伏的低模团块，
    颜色按深度从参考裁剪图上（远）到下（近）取样并量化成 colors 个色块；取景时忽略它。"""
    return _out(call("build_backdrop", size_m=size_m, image=image or None, mask=mask or None,
                     name=_safe_name(name), seed=seed, colors=colors, flat=bool(flat)))


@mcp.tool()
def blender_import_model(path: str) -> str:
    """导入 .glb/.gltf/.obj/.fbx，或从 .blend 追加对象到当前场景。"""
    return _out(call("import_model", path=str(Path(path).expanduser().resolve())))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        _ensure_current_worker()
    except Exception:  # 不阻止服务启动；首次调用时会再尝试启动 worker
        pass
    mcp.run("stdio")
