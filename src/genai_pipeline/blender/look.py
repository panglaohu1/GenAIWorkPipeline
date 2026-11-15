"""机位与光照预设（纯 Python，无 bpy）：Blender 渲染（worker.py）与 Godot 展示场景（oskar_godot 阶段）共用，保证两边同机位、同光照。

坐标：Blender Z 向上；glTF / Godot Y 向上，换算 (x, y, z)_blender → (x, z, -y)_godot（与 GLB 导出 export_yup 一致）。
"""
import math

# 经色卡校准：朝向主光的面接近材质本色，不过曝（见 tests/test_integration.py::test_render_color_fidelity）
WORLD_STRENGTH = 0.55
WORLD_COLOR = (0.32, 0.34, 0.38)
# (名称, 欧拉角 XYZ 度, 强度 W/m²)；灯光默认沿自身 -Z 照射
LIGHTS = (("_pipe_key", (50, 10, 35), 2.6), ("_pipe_fill", (60, 0, -120), 0.7), ("_pipe_rim", (-30, 0, 180), 1.0))

LIGHT_PRESETS = {
    # name: (world rgb, world strength, {light: (energy, rgb, rotation or None)})
    "studio": None,
    "day": ((0.62, 0.72, 0.85), 0.75, {"_pipe_key": (3.3, (1.0, 0.97, 0.92), (32, 6, 38)),
                                      "_pipe_fill": (0.55, (0.8, 0.88, 1.0), None), "_pipe_rim": (0.6, (1, 1, 1), None)}),
    "dusk": ((0.42, 0.44, 0.62), 0.55, {"_pipe_key": (2.6, (1.0, 0.72, 0.48), (74, 6, 55)),
                                        "_pipe_fill": (0.45, (0.62, 0.66, 1.0), None), "_pipe_rim": (0.9, (1.0, 0.8, 0.62), None)}),
    # 夜：冷蓝环境 + 很弱的月光；暖色由少量“大而柔”的点光（灯笼 / 巡逻火把）提供（x.com/OskSta 2022-10-19 光照体积）
    "night": ((0.1, 0.14, 0.28), 0.5, {"_pipe_key": (0.55, (0.62, 0.72, 1.0), (58, 6, 210)),
                                       "_pipe_fill": (0.2, (0.45, 0.55, 1.0), None), "_pipe_rim": (0.25, (0.6, 0.7, 1.0), None)}),
}

VIEW_DIRS = {
    "front": (0, -1, 0), "back": (0, 1, 0), "side": (1, 0, 0), "left": (-1, 0, 0),
    "top": (0, 0, 1), "persp": (1, -1.2, 0.8), "persp_back": (-1, 1.2, 0.8),
    "persp_left": (-1.1, -1.0, 0.9),  # 左前方 3/4 俯视，贴近常见概念图视角
    "hero": (0.55, -1.5, 0.42),         # 低机位 3/4：看轮廓主次与台面层次
    "hero_left": (-0.7, -1.45, 0.45),
}
CAMERA_LENS = 50        # mm，传感器 36 mm → 视场角 2·atan(18/50) ≈ 39.6°
FRAME_MARGIN = 1.05     # 取景距离 = 包围球半径 / tan(半视场角) × 此系数


def camera_fov_deg() -> float:
    return math.degrees(2 * math.atan(18 / CAMERA_LENS))


def sun_direction(rot_deg) -> tuple:
    """Blender 太阳灯欧拉角（XYZ，度）→ 光线传播方向（Blender 坐标，单位向量）：R = Rz·Ry·Rx 作用于 (0,0,-1)。"""
    a, b, c = (math.radians(v) for v in rot_deg)
    x, y, z = 0.0, 0.0, -1.0
    y, z = y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)
    x, z = x * math.cos(b) + z * math.sin(b), -x * math.sin(b) + z * math.cos(b)
    x, y = x * math.cos(c) - y * math.sin(c), x * math.sin(c) + y * math.cos(c)
    return (x, y, z)


def to_godot(v) -> list:
    return [round(v[0], 5), round(v[2], 5), round(-v[1], 5)]


def godot_lighting(preset: str) -> dict:
    """某个预设在 Godot 中的等价参数。颜色为线性值，由 GDScript 转 sRGB；Blender 太阳强度 E 对应 Godot 能量 E/π。"""
    cfg = LIGHT_PRESETS.get(preset)
    lights = []
    for name, rot, energy in LIGHTS:
        e, col, r2 = (energy, (1, 1, 1), None) if cfg is None else cfg[2].get(name, (energy, (1, 1, 1), None))
        lights.append({"name": name, "dir": to_godot(sun_direction(r2 or rot)), "energy": round(e / math.pi, 4),
                       "color": list(col), "shadow": True})
    bg, strength = (WORLD_COLOR, WORLD_STRENGTH) if cfg is None else (cfg[0], cfg[1])
    return {"bg": list(bg), "bg_strength": strength, "lights": lights}
