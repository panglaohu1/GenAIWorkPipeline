"""在 Blender 内运行的工作进程：本地 TCP 行协议 JSON-RPC。

启动（由 blender MCP 服务端负责）:
  Blender -b --factory-startup --python worker.py -- --port 9877 [--workspace DIR]
  （去掉 -b 即为有界面模式，用 bpy.app.timers 轮询，用户可实时观看）

请求: {"id": 1, "op": "exec", "args": {...}}\n
响应: {"id": 1, "ok": true, "result": ..., "stdout": "..."}\n
"""
import contextlib
import io
import json
import math
import os
import socket
import sys
import traceback

import bmesh  # noqa: F401  (暴露给模型代码)
import bpy
import mathutils
from mathutils import Euler, Matrix, Vector  # noqa: F401

_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from genai_pipeline.blender.look import (CAMERA_LENS, FRAME_MARGIN, LIGHT_PRESETS, LIGHTS,  # noqa: E402
                                         VIEW_DIRS, WORLD_COLOR, WORLD_STRENGTH)


def _argv():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = {"port": 9877, "workspace": os.getcwd()}
    for i, k in enumerate(a):
        if k == "--port":
            out["port"] = int(a[i + 1])
        if k == "--workspace":
            out["workspace"] = a[i + 1]
    return out


ARGS = _argv()


# ───────────────────────── 建模辅助库（在 run_python 中以 H 访问） ─────────────────────────
class H:
    """简洁建模助手。单位：米；角度：度。所有函数返回创建的对象。"""

    @staticmethod
    def clear():
        """删除场景中所有对象和孤立数据。"""
        bpy.ops.object.select_all(action="DESELECT")
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
        for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.curves, bpy.data.cameras, bpy.data.lights):
            for d in list(coll):
                if d.users == 0:
                    coll.remove(d)

    @staticmethod
    def mat(name, color=(0.8, 0.8, 0.8), metallic=0.0, roughness=0.5, emission=None, emission_strength=0.0,
            alpha=1.0):
        """创建/复用 Principled 材质。color 为 RGB 0-1 或 '#rrggbb'。"""
        if isinstance(color, str):
            color = H.hex(color)
        m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get("Principled BSDF")
        rgba = (*color[:3], 1.0)
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Alpha"].default_value = alpha
        if emission:
            ec = H.hex(emission) if isinstance(emission, str) else emission
            bsdf.inputs["Emission Color"].default_value = (*ec[:3], 1.0)
            bsdf.inputs["Emission Strength"].default_value = emission_strength or 1.0
        if alpha < 1.0:
            # Blender 4.2+ EEVEE：透明需要非 OPAQUE 的表面渲染方式（玻璃罩、水面等）
            with contextlib.suppress(AttributeError, TypeError):
                m.surface_render_method = "BLENDED"
            with contextlib.suppress(AttributeError, TypeError):
                m.blend_method = "BLEND"
        m.diffuse_color = (*color[:3], alpha)
        m.metallic = metallic
        m.roughness = roughness
        return m

    @staticmethod
    def hex(h):
        h = str(h).strip().lstrip("#")
        if len(h) == 3:  # #eee 简写
            h = "".join(c * 2 for c in h)
        try:
            srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        except ValueError:
            srgb = [0.8, 0.8, 0.8]  # 无法解析的颜色用浅灰，不让整个部件失败
        return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb)

    @staticmethod
    def _finish(o, name, location, rotation, material, parent):
        if name:
            o.name = name
            if o.data is not None and hasattr(o.data, "name"):
                o.data.name = name
        o.location = location
        o.rotation_euler = [math.radians(a) for a in rotation]
        if material is not None:
            H.assign(o, material)
        if parent is not None:
            o.parent = parent
        return o

    @staticmethod
    def box(name=None, size=(1, 1, 1), location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None,
            bevel=0.0):
        bpy.ops.mesh.primitive_cube_add(size=1)
        o = bpy.context.active_object
        o.scale = size
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        if bevel:
            H.bevel(o, bevel)
        return H._finish(o, name, location, rotation, material, parent)

    @staticmethod
    def cylinder(name=None, radius=0.5, depth=1.0, location=(0, 0, 0), rotation=(0, 0, 0), material=None,
                 parent=None, vertices=24):
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, vertices=vertices)
        return H._finish(bpy.context.active_object, name, location, rotation, material, parent)

    @staticmethod
    def cone(name=None, radius1=0.5, radius2=0.0, depth=1.0, location=(0, 0, 0), rotation=(0, 0, 0),
             material=None, parent=None, vertices=24):
        bpy.ops.mesh.primitive_cone_add(radius1=radius1, radius2=radius2, depth=depth, vertices=vertices)
        return H._finish(bpy.context.active_object, name, location, rotation, material, parent)

    @staticmethod
    def sphere(name=None, radius=0.5, location=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1), material=None,
               parent=None, segments=24, rings=12):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, segments=segments, ring_count=rings)
        o = bpy.context.active_object
        o.scale = scale
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bpy.ops.object.shade_smooth()
        return H._finish(o, name, location, rotation, material, parent)

    @staticmethod
    def torus(name=None, major=0.5, minor=0.1, location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None):
        bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor)
        return H._finish(bpy.context.active_object, name, location, rotation, material, parent)

    @staticmethod
    def plane(name=None, size=1.0, location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None):
        bpy.ops.mesh.primitive_plane_add(size=size)
        return H._finish(bpy.context.active_object, name, location, rotation, material, parent)

    @staticmethod
    def mesh(name, verts, faces, location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None):
        """由顶点/面列表创建网格（自定义形状）。"""
        me = bpy.data.meshes.new(name)
        me.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in faces])
        me.update()
        o = bpy.data.objects.new(name, me)
        bpy.context.collection.objects.link(o)
        return H._finish(o, name, location, rotation, material, parent)

    @staticmethod
    def extrude_profile(name, points2d, depth, location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None):
        """把 XZ 平面上的闭合 2D 轮廓沿 Y 方向挤出 depth（适合剪影类形状）。"""
        n = len(points2d)
        verts = [(x, -depth / 2, z) for x, z in points2d] + [(x, depth / 2, z) for x, z in points2d]
        faces = [list(range(n))[::-1], list(range(n, 2 * n))]
        faces += [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)]
        o = H.mesh(name, verts, faces, location, rotation, material, parent)
        return o

    @staticmethod
    def lathe(name, profile, segments=32, location=(0, 0, 0), rotation=(0, 0, 0), material=None, parent=None):
        """把 (r, z) 轮廓绕 Z 轴旋转成回转体（瓶、柱、树干等）。"""
        verts, faces = [], []
        m = len(profile)
        for s in range(segments):
            a = 2 * math.pi * s / segments
            for r, z in profile:
                verts.append((r * math.cos(a), r * math.sin(a), z))
        for s in range(segments):
            for i in range(m - 1):
                a0, a1 = s * m + i, ((s + 1) % segments) * m + i
                faces.append([a0, a1, a1 + 1, a0 + 1])
        o = H.mesh(name, verts, faces, location, rotation, material, parent)
        H.smooth(o)
        return o

    @staticmethod
    def assign(o, material):
        if isinstance(material, str):
            material = bpy.data.materials.get(material) or H.mat(material)
        if o.data is not None and hasattr(o.data, "materials"):
            o.data.materials.clear()
            o.data.materials.append(material)
        return o

    @staticmethod
    def bevel(o, width=0.02, segments=2):
        m = o.modifiers.new("Bevel", "BEVEL")
        m.width = width
        m.segments = segments
        m.limit_method = "ANGLE"
        return o

    @staticmethod
    def subsurf(o, levels=1):
        m = o.modifiers.new("Subsurf", "SUBSURF")
        m.levels = levels
        m.render_levels = levels
        return o

    @staticmethod
    def mirror(o, axis="X"):
        m = o.modifiers.new("Mirror", "MIRROR")
        m.use_axis = [axis == "X", axis == "Y", axis == "Z"]
        return o

    @staticmethod
    def array(o, count=3, offset=(1.2, 0, 0)):
        m = o.modifiers.new("Array", "ARRAY")
        m.count = count
        m.relative_offset_displace = offset
        return o

    @staticmethod
    def smooth(o):
        for p in o.data.polygons:
            p.use_smooth = True
        return o

    @staticmethod
    def select_only(objs):
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]

    @staticmethod
    def apply_modifiers(o=None):
        objs = [o] if o else [x for x in bpy.data.objects if x.type == "MESH"]
        for x in objs:
            H.select_only([x])
            for m in list(x.modifiers):
                bpy.ops.object.modifier_apply(modifier=m.name)
        return objs

    @staticmethod
    def join(objs, name):
        objs = [o for o in objs if o.type == "MESH"]
        H.select_only(objs)
        bpy.ops.object.join()
        o = bpy.context.active_object
        o.name = name
        return o

    @staticmethod
    def empty(name, location=(0, 0, 0), parent=None):
        o = bpy.data.objects.new(name, None)
        bpy.context.collection.objects.link(o)
        o.location = location
        if parent is not None:
            o.parent = parent
        return o

    @staticmethod
    def get(name):
        """按名称取对象；没有同名对象时取同名材质（模型常用 H.get 取材质）。"""
        return bpy.data.objects.get(name) or bpy.data.materials.get(name)

    @staticmethod
    def delete(name_or_obj):
        o = bpy.data.objects.get(name_or_obj) if isinstance(name_or_obj, str) else name_or_obj
        if o is not None:
            bpy.data.objects.remove(o, do_unlink=True)

    @staticmethod
    def limb(name=None, start=(0, 0, 0), end=(0, 0, 1), radius=0.1, radius_end=None, material=None, parent=None,
             vertices=12):
        """两点之间的圆柱/锥形肢体（腿、手臂、尾巴、脖子、支架）。不用手算旋转角。
        radius_end 不同于 radius 时为锥形（如尾巴末端变细）。"""
        a, b = Vector(start), Vector(end)
        d = b - a
        length = max(d.length, 1e-4)
        r2 = radius if radius_end is None else radius_end
        bpy.ops.mesh.primitive_cone_add(radius1=radius, radius2=r2, depth=length, vertices=vertices)
        o = bpy.context.active_object
        o.location = (a + b) / 2
        o.rotation_euler = d.to_track_quat("Z", "Y").to_euler()
        if name:
            o.name = name
            o.data.name = name
        if material is not None:
            H.assign(o, material)
        if parent is not None:
            o.parent = parent
        return o

    @staticmethod
    def capsule(name=None, radius=0.2, length=0.6, location=(0, 0, 0), rotation=(0, 0, 0), material=None,
                parent=None, axis="Z"):
        """胶囊体（圆润的躯干、四肢、头部）。length 为总长（含两端半球），axis 为长轴方向。"""
        cyl = max(length - 2 * radius, 0.0)
        prof = []
        n = 6
        for i in range(n + 1):  # 下半球
            t = -math.pi / 2 + (math.pi / 2) * i / n
            prof.append((radius * math.cos(t), -cyl / 2 + radius * math.sin(t)))
        for i in range(n + 1):  # 上半球
            t = (math.pi / 2) * i / n
            prof.append((radius * math.cos(t), cyl / 2 + radius * math.sin(t)))
        prof[0] = (0.0, prof[0][1])
        prof[-1] = (0.0, prof[-1][1])
        o = H.lathe(name or "capsule", prof, segments=16, material=material, parent=parent)
        if axis == "X":
            o.rotation_euler = (0, math.radians(90), 0)
        elif axis == "Y":
            o.rotation_euler = (math.radians(90), 0, 0)
        bpy.context.view_layer.objects.active = o
        H.select_only([o])
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        o.location = location
        o.rotation_euler = [math.radians(v) for v in rotation]
        return o

    @staticmethod
    def rounded_box(name=None, size=(1, 1, 1), radius=0.08, location=(0, 0, 0), rotation=(0, 0, 0),
                    material=None, parent=None):
        """圆角盒（玩具感的头、身体、方块部件）。"""
        o = H.box(name, size=size, location=location, rotation=rotation, material=material, parent=parent)
        m = o.modifiers.new("Round", "BEVEL")
        m.width = radius
        m.segments = 3
        return o

    @staticmethod
    def bounds(o):
        """返回对象（含子对象）的世界包围盒 {'min','max','size','center'}（米）。"""
        objs = [o] + [c for c in o.children_recursive if c.type == "MESH"] if o.type != "MESH" else [o]
        dg = bpy.context.evaluated_depsgraph_get()
        mn = Vector((1e9, 1e9, 1e9))
        mx = Vector((-1e9, -1e9, -1e9))
        for x in objs:
            if x.type != "MESH":
                continue
            xe = x.evaluated_get(dg)
            for c in xe.bound_box:
                w = xe.matrix_world @ Vector(c)
                mn = Vector(map(min, mn, w))
                mx = Vector(map(max, mx, w))
        return {"min": tuple(round(v, 4) for v in mn), "max": tuple(round(v, 4) for v in mx),
                "size": tuple(round(b - a, 4) for a, b in zip(mn, mx)),
                "center": tuple(round((a + b) / 2, 4) for a, b in zip(mn, mx))}

    @staticmethod
    def snap(obj, to, side="+X", overlap=0.0, offset=(0, 0, 0)):
        """把 obj 贴到 to 的某一侧：side 取 '+X','-X','+Y','-Y','+Z','-Z'。
        obj 在该轴上紧贴 to 的表面（overlap>0 表示嵌入一点避免缝隙），其余两轴与 to 中心对齐，再加 offset。
        例：H.snap(tail_fin, body, '-X', overlap=0.05)  把尾翼接到机身后端；
            H.snap(roof, walls, '+Z')                   屋顶放在墙体正上方。"""
        bpy.context.view_layer.update()
        tb, ob = H.bounds(to), H.bounds(obj)
        axis = "XYZ".index(side[1].upper())
        sign = 1 if side[0] == "+" else -1
        delta = [tb["center"][i] - ob["center"][i] for i in range(3)]
        if sign > 0:
            delta[axis] = tb["max"][axis] - ob["min"][axis] - overlap
        else:
            delta[axis] = tb["min"][axis] - ob["max"][axis] + overlap
        obj.location = obj.location + Vector(delta) + Vector(offset)
        bpy.context.view_layer.update()
        return obj

    @staticmethod
    def ground_to_origin():
        """把所有网格整体平移，使底部落在 z=0、XY 居中（导出游戏资产前推荐）。"""
        mn, mx = _bounds()
        if mn is None:
            return
        shift = Vector((-(mn.x + mx.x) / 2, -(mn.y + mx.y) / 2, -mn.z))
        for o in bpy.data.objects:
            if o.parent is None:
                o.location += shift


_ALIASES = {"subdivisions": "segments", "segs": "segments", "verts": "vertices", "loc": "location",
            "pos": "location", "position": "location", "rot": "rotation", "mat": "material", "radius_top": "radius2",
            "radius_bottom": "radius1", "height": "depth", "length": "depth", "dims": "size", "dimensions": "size"}


def _tolerant(fn):
    """H 助手容错：常见别名自动映射，未知参数忽略并打印提示（而不是整段代码失败）。"""
    import functools
    import inspect
    sig = inspect.signature(fn)
    names = set(sig.parameters)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        fixed = {}
        for k, v in kwargs.items():
            k2 = k if k in names else _ALIASES.get(k, k)
            if k2 in names:
                fixed[k2] = v
            else:
                print(f"[H.{fn.__name__}] 忽略未知参数 {k}（可用: {', '.join(sorted(names))}）")
        return fn(*args, **fixed)
    return staticmethod(wrapper)


for _n, _f in list(vars(H).items()):
    if isinstance(_f, staticmethod) and not _n.startswith("_"):
        setattr(H, _n, _tolerant(_f.__func__))


def _install_conveniences():
    """把模型最常见的误用变成可用写法（实测反复出现），减少失败步数。"""
    import types
    mod = types.ModuleType("H")  # 支持 `import H` / `from H import box`
    for k in dir(H):
        if not k.startswith("_"):
            setattr(mod, k, getattr(H, k))
    sys.modules["H"] = mod

    def _get_mat(self):
        return self.active_material

    def _set_mat(self, m):
        H.assign(self, m)

    def _get_rot(self):
        return tuple(math.degrees(a) for a in self.rotation_euler)

    def _set_rot(self, v):  # obj.rotation = (x, y, z) 按度处理，与 H 助手一致
        self.rotation_euler = [math.radians(a) for a in v]

    try:
        bpy.types.Object.material = property(_get_mat, _set_mat)
        bpy.types.Object.rotation = property(_get_rot, _set_rot)
    except (AttributeError, TypeError):
        pass


_install_conveniences()


# ───────────────────────── 场景查询 / 渲染 / 导出 ─────────────────────────
def _mesh_objects():
    return [o for o in bpy.context.scene.objects
            if o.type == "MESH" and o.visible_get() and not o.name.startswith("_pipe_")]


def _bounds(framing=False):
    objs = _mesh_objects()
    if framing:  # 取景时忽略海面等大平面（ignore_bounds），让主体填满画面
        objs = [o for o in objs if not o.get("ignore_bounds")] or objs
    if not objs:
        return None, None
    dg = bpy.context.evaluated_depsgraph_get()
    mn = Vector((1e9, 1e9, 1e9))
    mx = Vector((-1e9, -1e9, -1e9))
    for o in objs:
        oe = o.evaluated_get(dg)
        for c in oe.bound_box:
            w = oe.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
    return mn, mx


def scene_info():
    dg = bpy.context.evaluated_depsgraph_get()
    objs = []
    tris = 0
    nonfinite = []
    for o in bpy.context.scene.objects:
        if o.name.startswith("_pipe_"):
            continue
        entry = {"name": o.name, "type": o.type, "parent": o.parent.name if o.parent else None,
                 "location": [round(v, 3) for v in o.location],
                 "dimensions": [round(v, 3) for v in o.dimensions]}
        if not all(math.isfinite(v) for row in o.matrix_world for v in row):
            nonfinite.append(o.name)
        if o.type == "MESH":
            oe = o.evaluated_get(dg)
            me = oe.to_mesh()
            t = sum(len(p.vertices) - 2 for p in me.polygons)
            oe.to_mesh_clear()
            tris += t
            entry["triangles"] = t
            entry["materials"] = [m.name for m in o.data.materials if m]
            entry["modifiers"] = [m.type for m in o.modifiers]
        objs.append(entry)
    mn, mx = _bounds()
    return {
        "object_count": len(objs), "triangles": tris,
        "bounds_min": [round(v, 3) for v in mn] if mn else None,
        "bounds_max": [round(v, 3) for v in mx] if mx else None,
        "size": [round(b - a, 3) for a, b in zip(mn, mx)] if mn else None,
        "materials": [m.name for m in bpy.data.materials if m.users],
        "nonfinite_transforms": nonfinite,
        "objects": objs[:80], "truncated": len(objs) > 80,
    }


def _engine(pref):
    items = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    for cand in (pref, "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"):
        if cand in items:
            return cand
    return items[0]


def _apply_lighting(scene, preset):
    cfg = LIGHT_PRESETS.get(preset or "studio")
    world = scene.world
    bg = world.node_tree.nodes.get("Background") if world and world.use_nodes else None
    for name, rot, energy in LIGHTS:
        o = bpy.data.objects.get(name)
        if o is None:
            continue
        if cfg is None:
            o.data.energy, o.data.color = energy, (1, 1, 1)
            o.rotation_euler = [math.radians(a) for a in rot]
            continue
        e, col, r2 = cfg[2].get(name, (energy, (1, 1, 1), None))
        o.data.energy, o.data.color = e, col
        o.rotation_euler = [math.radians(a) for a in (r2 or rot)]
    if bg:
        if cfg is None:
            bg.inputs[0].default_value = (*WORLD_COLOR, 1)
            bg.inputs[1].default_value = WORLD_STRENGTH
        else:
            bg.inputs[0].default_value = (*cfg[0], 1)
            bg.inputs[1].default_value = cfg[1]


def _setup_studio(scene, engine, size):
    scene.render.engine = _engine(engine)
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.render.film_transparent = False
    # Standard 色彩映射：保证材质颜色与概念图可比（AgX 会压低饱和度）
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except TypeError:
        pass
    if scene.render.engine == "BLENDER_WORKBENCH":
        sh = scene.display.shading
        sh.light = "STUDIO"
        sh.color_type = "MATERIAL"
        sh.show_cavity = True
    else:
        try:
            scene.eevee.taa_render_samples = 16
        except AttributeError:
            pass
    world = scene.world or bpy.data.worlds.new("PipelineWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (*WORLD_COLOR, 1)
        bg.inputs[1].default_value = WORLD_STRENGTH
    for name, rot, energy in LIGHTS:
        if name not in bpy.data.objects:
            ld = bpy.data.lights.new(name, "SUN")
            ld.energy = energy
            lo = bpy.data.objects.new(name, ld)
            scene.collection.objects.link(lo)
            lo.rotation_euler = [math.radians(a) for a in rot]
            lo.hide_select = True


def _apply_rig(scene, rig):
    """布光：按实测的主光 / 补光 / 环境把灯光组摆好。rig = {"key": {"dir": 指向光源的世界向量, "color": 线性 rgb, "energy"},
    "fill": {...}, "rim": {...}, "world": {"color": 线性 rgb, "strength"}}；缺的项保持预设。"""
    for name, part in (("_pipe_key", "key"), ("_pipe_fill", "fill"), ("_pipe_rim", "rim")):
        cfg = rig.get(part) or {}
        o = bpy.data.objects.get(name)
        if o is None or not cfg:
            continue
        if cfg.get("energy") is not None:
            o.data.energy = max(0.0, float(cfg["energy"]))
        if cfg.get("color"):
            o.data.color = tuple(max(0.0, float(c)) for c in cfg["color"][:3])
        d = cfg.get("dir")
        if d:
            v = Vector([float(x) for x in d])
            if v.length > 1e-6:
                o.rotation_euler = (-v.normalized()).to_track_quat("-Z", "Y").to_euler()
        if cfg.get("angle_deg") is not None and hasattr(o.data, "angle"):
            o.data.angle = math.radians(float(cfg["angle_deg"]))   # 太阳的视角大小 = 影子边缘的软硬
    w = rig.get("world") or {}
    bg = scene.world.node_tree.nodes.get("Background") if scene.world and scene.world.use_nodes else None
    if bg is not None:
        if w.get("color"):
            bg.inputs[0].default_value = (*[max(0.0, float(c)) for c in w["color"][:3]], 1)
        if w.get("strength") is not None:
            bg.inputs[1].default_value = max(0.0, float(w["strength"]))


def _sky_texture(nt, sky):
    """天空盒（等距柱状图）节点：同一张图既给相机当背景，也可给环境光取色。"""
    node = nt.nodes.get("_pipe_sky_tex")
    if not sky:
        return None
    if node is None:
        node = nt.nodes.new("ShaderNodeTexEnvironment")
        node.name = "_pipe_sky_tex"
    path = os.path.abspath(sky)
    img = node.image
    if img is None or os.path.abspath(bpy.path.abspath(img.filepath)) != path:
        img = bpy.data.images.load(path, check_existing=True)
        img.reload()
        node.image = img
    return node


def render_views(views, out_dir, size=768, engine="BLENDER_EEVEE", prefix="view", lighting=None, camera=None,
                 backdrop=None, sun_dir=None, exposure=None, sky=None, rig=None):
    """camera = {"eye": [x,y,z], "target": [x,y,z], "fov_deg": 40, "aspect": 宽/高}：视图名 "ref" 用这个参考机位
    （按参考图的取景位置与画幅渲染，供与参考图同机位对比）；backdrop = [r,g,b]（0–1，sRGB）作为背景色。"""
    scene = bpy.context.scene
    mn, mx = _bounds(framing=True)
    if mn is None:
        raise RuntimeError("场景中没有可见网格，无法渲染")
    _setup_studio(scene, engine, size)
    _apply_lighting(scene, lighting)
    if rig:
        _apply_rig(scene, rig)
    # 导入的 GLB 不带「背面不投影」：描边外壳（单面、法线朝里）会把整件物体罩进自己的阴影里，主光照不到
    #（实测：组装场景里主光强度 1 和 6 渲出来几乎一样）。单面材质一律不让背面投影
    for m in bpy.data.materials:
        if getattr(m, "use_backface_culling", False) and hasattr(m, "use_backface_culling_shadow"):
            m.use_backface_culling_shadow = True
    # 曝光（档）：组装时按配色验收测得的「目标图 / 渲染图」亮度比逐轮校正；不给时归零，不影响单物件渲染
    scene.view_settings.exposure = max(-3.0, min(3.0, float(exposure))) if exposure else 0.0
    if sun_dir:
        # 主光方向取配色环节从目标图测得的（世界系、指向光源）：灯沿自身 -Z 照射，所以让 -Z 指向 -sun_dir
        key = bpy.data.objects.get("_pipe_key")
        d = Vector(sun_dir)
        if key is not None and d.length > 1e-6:
            key.rotation_euler = (-d.normalized()).to_track_quat("-Z", "Y").to_euler()
    if (backdrop or sky) and scene.world and scene.world.node_tree:
        backdrop = backdrop or [0.6, 0.8, 0.95]
        nt = scene.world.node_tree
        bg = nt.nodes.get("Background")
        if bg:
            lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in backdrop[:3]]
            # 背景色只给相机看（强度 1，和参考图天空一致）；照明仍用灯光预设的环境色与强度，天色不再把整个场景染蓝压暗
            cam_bg = nt.nodes.get("_pipe_cam_bg") or nt.nodes.new("ShaderNodeBackground")
            cam_bg.name = "_pipe_cam_bg"
            cam_bg.inputs[0].default_value = (*lin, 1)
            # 背景是直接看到的天色（目标图实测），不该跟着曝光变：曝光加 n 档，背景强度就减 n 档
            cam_bg.inputs[1].default_value = 2.0 ** -scene.view_settings.exposure
            tex = _sky_texture(nt, sky)
            for lk in list(cam_bg.inputs[0].links):
                nt.links.remove(lk)
            for lk in list(bg.inputs[0].links):
                nt.links.remove(lk)
            if tex is not None:
                # 天空盒：相机看到的是整张全景（天的渐变 + 远海），不再是一块纯色；环境光按 rig 选择是否也取天空盒的颜色
                nt.links.new(tex.outputs["Color"], cam_bg.inputs[0])
                if (rig or {}).get("world", {}).get("sky_light"):
                    nt.links.new(tex.outputs["Color"], bg.inputs[0])
            lp = nt.nodes.get("_pipe_lp") or nt.nodes.new("ShaderNodeLightPath")
            lp.name = "_pipe_lp"
            mix = nt.nodes.get("_pipe_mix") or nt.nodes.new("ShaderNodeMixShader")
            mix.name = "_pipe_mix"
            out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None)
            if out is not None:
                nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
                nt.links.new(bg.outputs[0], mix.inputs[1])
                nt.links.new(cam_bg.outputs[0], mix.inputs[2])
                nt.links.new(mix.outputs[0], out.inputs["Surface"])
    elif scene.world and scene.world.node_tree:
        # 没给背景色：恢复成单一环境（别让上一次的参考天色留在资产审查图里）
        nt = scene.world.node_tree
        bg, out = nt.nodes.get("Background"), next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None)
        if bg is not None and out is not None and nt.nodes.get("_pipe_mix") is not None:
            nt.links.new(bg.outputs[0], out.inputs["Surface"])
        if bg is not None:
            for lk in list(bg.inputs[0].links):   # 上一次组装接上的天空盒取色也撤掉
                nt.links.remove(lk)
    center = (mn + mx) / 2
    radius = max((mx - mn).length / 2, 0.05)
    cam_data = bpy.data.cameras.get("_pipe_cam") or bpy.data.cameras.new("_pipe_cam")
    cam = bpy.data.objects.get("_pipe_cam") or bpy.data.objects.new("_pipe_cam", cam_data)
    if cam.name not in scene.collection.objects:
        scene.collection.objects.link(cam)
    cam_data.lens = CAMERA_LENS
    cam_data.clip_end = radius * 100
    scene.camera = cam
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for v in views:
        scene.render.resolution_x = scene.render.resolution_y = size
        if v == "ref" and camera:
            fov = math.radians(max(10.0, min(100.0, float(camera.get("fov_deg", 40)))))
            aspect = max(0.4, min(4.0, float(camera.get("aspect", 1.0))))
            scene.render.resolution_x, scene.render.resolution_y = size, max(64, int(round(size / aspect)))
            cam_data.sensor_fit = "HORIZONTAL"
            cam_data.lens = 18 / math.tan(fov / 2)
            cam.location = Vector([float(x) for x in camera["eye"]])
            cam.rotation_euler = (Vector([float(x) for x in camera["target"]]) - cam.location).to_track_quat("-Z", "Y").to_euler()
            path = os.path.join(out_dir, f"{prefix}_{v}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            paths.append(path)
            cam_data.lens = CAMERA_LENS
            continue
        d = Vector(VIEW_DIRS.get(v, VIEW_DIRS["persp"])).normalized()
        fov = 2 * math.atan(18 / cam_data.lens)
        dist = radius / math.tan(fov / 2) * FRAME_MARGIN
        cam.location = center + d * dist
        if v == "top":
            cam.rotation_euler = Euler((0, 0, 0))  # 相机默认朝 -Z，即俯视
        else:
            cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
        path = os.path.join(out_dir, f"{prefix}_{v}.png")
        scene.render.filepath = path
        try:
            bpy.ops.render.render(write_still=True)
        except RuntimeError:
            scene.render.engine = "BLENDER_WORKBENCH"
            _setup_studio(scene, "BLENDER_WORKBENCH", size)
            bpy.ops.render.render(write_still=True)
        paths.append(path)
    return {"paths": paths, "engine": scene.render.engine}


# ───────────────────────── 蓝图建模：结构化部件清单 → 确定性构建 ─────────────────────────
BLUEPRINT_SHAPES = {"box", "rounded_box", "sphere", "capsule", "cylinder", "cone", "limb", "torus", "dome", "rock"}
_AXIS_ROT = {"X": (0, 90, 0), "Y": (90, 0, 0), "Z": (0, 0, 0)}


def _num(v, default):
    """模型常把标量写成列表/字符串：取列表最小值、字符串转数字，失败用默认值。"""
    if isinstance(v, (list, tuple)):
        vals = [x for x in (_num(x, None) for x in v) if x is not None]
        return min(vals) if vals else default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _vec3(v, default):
    if isinstance(v, (int, float)):
        return [float(v)] * 3
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return [_num(x, d) for x, d in zip(v[:3], default)]
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return [_num(v[0], default[0]), _num(v[1], default[1]), default[2]]
    return list(default)


def _bp_normalize(p):
    for k in ("radius", "radius1", "radius2", "radius_end", "length", "depth", "major", "minor", "roughness",
              "metallic", "alpha", "overlap"):
        if k in p and p[k] is not None:
            p[k] = _num(p[k], None)
    for k in ("size",):
        if k in p:
            p[k] = [max(1e-3, abs(x)) for x in _vec3(p[k], [0.2, 0.2, 0.2])]
    for k in ("at", "from", "to", "rotation"):
        if k in p:
            p[k] = _vec3(p[k], [0, 0, 0])
    at = p.get("attach")
    if isinstance(at, dict):
        if "overlap" in at:
            at["overlap"] = _num(at["overlap"], 0.01)
        if "offset" in at:
            at["offset"] = _vec3(at["offset"], [0, 0, 0])
    arr = p.get("array")
    if isinstance(arr, dict):
        arr["count"] = int(_num(arr.get("count"), 1))
        arr["offset"] = _vec3(arr.get("offset"), [1, 0, 0])
    return p


def _bp_make(p, mat):
    _bp_normalize(p)
    shape = p.get("shape", "box")
    name = p["name"]
    size = p.get("size", [0.2, 0.2, 0.2])
    axis = str(p.get("axis", "Z")).upper()
    if shape == "box":
        return H.box(name, size=size, material=mat)
    if shape == "rounded_box":
        r = p.get("radius") or min(size) * 0.2
        return H.rounded_box(name, size=size, radius=min(r, min(size) * 0.45), material=mat)
    if shape == "sphere":  # size = 椭球直径 (x, y, z)
        return H.sphere(name, radius=0.5, scale=size, material=mat, segments=20, rings=10)
    if shape == "capsule":
        return H.capsule(name, radius=p.get("radius", 0.1), length=p.get("length", 0.4), axis=axis, material=mat)
    if shape in ("cylinder", "cone"):
        r1 = p.get("radius", p.get("radius1", 0.1))
        r2 = r1 if shape == "cylinder" else p.get("radius2", 0.0)
        o = H.cone(name, radius1=r1, radius2=r2, depth=p.get("depth", 0.3), material=mat,
                   vertices=int(p.get("vertices", 16)))
        if axis != "Z":
            o.rotation_euler = [math.radians(a) for a in _AXIS_ROT[axis]]
            H.select_only([o])
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        return o
    if shape == "limb":
        return H.limb(name, start=p["from"], end=p["to"], radius=p.get("radius", 0.05),
                      radius_end=p.get("radius_end"), material=mat)
    if shape == "dome":  # 半球穹顶，size=[直径x, 直径y, 高z]，底面在部件中心下方（原点在底面中心）
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1, segments=24, ring_count=12)
        o = bpy.context.active_object
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z < -1e-4], context="VERTS")
        bm.to_mesh(o.data)
        bm.free()
        o.scale = (size[0] / 2, size[1] / 2, size[2])
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        o.data.transform(Matrix.Translation((0, 0, -size[2] / 2)))  # 与其他形状一致：原点在包围盒中心
        bpy.ops.object.shade_smooth()
        o.name = name
        H.assign(o, mat)
        return o
    if shape == "rock":  # 不规则低多边形岩块（悬崖、山体、礁石），size=[x,y,z]
        import random
        rnd = random.Random(hash(name) & 0xFFFF)
        bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5, subdivisions=2)
        o = bpy.context.active_object
        for v in o.data.vertices:
            v.co *= 1 + rnd.uniform(-0.18, 0.18)
        o.scale = size
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        o.name = name
        H.assign(o, mat)
        return o
    if shape == "torus":
        o = H.torus(name, major=p.get("major", 0.2), minor=p.get("minor", 0.04), material=mat)
        if axis != "Z":
            o.rotation_euler = [math.radians(a) for a in _AXIS_ROT[axis]]
            H.select_only([o])
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        return o
    raise ValueError(f"未知形状 {shape}")


def _touching(a, b, tol=0.015):
    return all(a["min"][i] <= b["max"][i] + tol and b["min"][i] <= a["max"][i] + tol for i in range(3))


def _floating_parts(built, boxes, tol):
    """悬空检测：包围盒粗筛 + 网格表面真实距离（BVH 最近点）。不规则岩块包围盒相交但实际没贴上的也能检出。"""
    from mathutils.bvhtree import BVHTree
    dg = bpy.context.evaluated_depsgraph_get()
    cache = {}

    def bvh_and_verts(n):
        if n not in cache:
            o = built[n]
            oe = o.evaluated_get(dg)
            me = oe.to_mesh()
            mw = oe.matrix_world
            verts = [mw @ v.co for v in me.vertices]
            polys = [tuple(p.vertices) for p in me.polygons]
            oe.to_mesh_clear()
            step = max(1, len(verts) // 300)
            cache[n] = (BVHTree.FromPolygons(verts, polys, all_triangles=False) if polys else None, verts[::step])
        return cache[n]

    names = list(boxes)
    if len(names) < 2:
        return []
    zmin = min(b["min"][2] for b in boxes.values())
    grounded = {n: boxes[n]["min"][2] <= zmin + tol for n in names}  # 落在场景底面的部件视为有支撑
    adj = {n: set() for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if grounded[a] and grounded[b]:
                continue
            if not _touching(boxes[a], boxes[b], tol):
                continue
            ta, va = bvh_and_verts(a)
            tb, vb = bvh_and_verts(b)
            hit = False
            if tb is not None:
                hit = any((r := tb.find_nearest(v, tol)) and r[0] is not None for v in va)
            if not hit and ta is not None:
                hit = any((r := ta.find_nearest(v, tol)) and r[0] is not None for v in vb)
            if not hit and ta is not None and tb is not None:
                hit = bool(ta.overlap(tb))
            if hit:
                adj[a].add(b)
                adj[b].add(a)
    # 连通块：着地部件经虚拟“地面”互连（多根柱子各自着地也算一体）；
    # 主体 = 总体积最大的连通块，不属于主体的部件才算悬空。
    # （旧逻辑要求连到最低点：飞机的最低点常是小装饰件，整架飞机被误判悬空）
    GROUND = "__ground__"
    adj[GROUND] = {n for n in names if grounded[n]}
    for n in adj[GROUND]:
        adj[n].add(GROUND)

    def vol(n):
        b = boxes[n]
        return max(1e-9, (b["max"][0] - b["min"][0]) * (b["max"][1] - b["min"][1]) * (b["max"][2] - b["min"][2]))

    comps, seen = [], set()
    for start in [GROUND] + names:
        if start in seen:
            continue
        comp, stack = {start}, [start]
        seen.add(start)
        while stack:
            for m in adj[stack.pop()]:
                if m not in seen:
                    seen.add(m)
                    comp.add(m)
                    stack.append(m)
        comps.append(comp - {GROUND})
    main = max(comps, key=lambda c: sum(vol(n) for n in c))
    return [n for n in names if n not in main]


def _gravity_snap(built, floating, boxes):
    """把悬空部件向下平移，使其底部落到正下方其他部件的表面上（射线检测）。返回已移动的部件名。"""
    from mathutils.bvhtree import BVHTree
    dg = bpy.context.evaluated_depsgraph_get()
    floating_set = set(floating)
    verts, polys = [], []
    for n, o in built.items():  # 只用非悬空部件构建“地面”
        if n in floating_set or o.type != "MESH":
            continue
        oe = o.evaluated_get(dg)
        me = oe.to_mesh()
        off = len(verts)
        verts += [oe.matrix_world @ v.co for v in me.vertices]
        polys += [tuple(i + off for i in p.vertices) for p in me.polygons]
        oe.to_mesh_clear()
    if not polys:
        return []
    tree = BVHTree.FromPolygons(verts, polys, all_triangles=False)
    moved = []
    for n in floating:
        b = boxes[n]
        best = None
        for fx in (0.5, 0.2, 0.8):  # 底面中心及两侧采样
            for fy in (0.5, 0.2, 0.8):
                origin = Vector((b["min"][0] + (b["max"][0] - b["min"][0]) * fx,
                                 b["min"][1] + (b["max"][1] - b["min"][1]) * fy, b["min"][2] + 1e-3))
                hit = tree.ray_cast(origin, Vector((0, 0, -1)), 1e4)
                if hit[0] is not None and (best is None or hit[3] < best):
                    best = hit[3]
        if best is not None and best > 1e-4:
            o = built[n]
            o.location.z -= best - 0.01  # 略微嵌入 1 cm
            moved.append(n)
    return moved


def build_blueprint(blueprint):
    """按蓝图重建场景。部件字段:
    name, shape(box|rounded_box|sphere|capsule|cylinder|cone|limb|torus), color('#rrggbb'),
    roughness, metallic, alpha, emission,
    尺寸: box/rounded_box/sphere 用 size=[x,y,z]；capsule 用 radius,length,axis；cylinder 用 radius,depth,axis；
          cone 用 radius1,radius2,depth,axis；limb 用 from,to,radius,radius_end；torus 用 major,minor,axis
    位置（二选一）: at=[x,y,z] 中心坐标；或 attach={"to": 部件名, "side": "+Z", "overlap": 0.02, "offset": [x,y,z]}
    rotation=[x,y,z] 度（在定位前应用，limb 不需要）；mirror="X" 在 x 取反处生成左右对称副本，"Y" 为前后对称
    """
    H.clear()
    parts = blueprint.get("parts", [])
    built, warnings, errors = {}, [], []
    mats = {}
    for p in parts:
        name = str(p.get("name") or f"part{len(built)}")
        p["name"] = name
        try:
            _bp_normalize(p)
            if p.get("shape", "box") not in BLUEPRINT_SHAPES:
                raise ValueError(f"未知形状 {p.get('shape')}，可用 {sorted(BLUEPRINT_SHAPES)}")
            key = (str(p.get("color") or "#cccccc"), p.get("roughness") if p.get("roughness") is not None else 0.6,
                   p.get("metallic") or 0.0, p.get("alpha") if p.get("alpha") is not None else 1.0,
                   p.get("emission") if isinstance(p.get("emission"), str) else None)
            if key not in mats:
                mats[key] = H.mat(f"m_{len(mats)}_{str(key[0]).lstrip('#')}", color=key[0], roughness=key[1],
                                  metallic=key[2], alpha=key[3], emission=key[4],
                                  emission_strength=2.0 if key[4] else 0.0)
            o = _bp_make(p, mats[key])
            if p.get("rotation") and p.get("shape") != "limb":
                o.rotation_euler = [math.radians(a) for a in p["rotation"]]
            if p.get("shape") != "limb":
                if "attach" in p:
                    at = p["attach"]
                    tgt = built.get(at.get("to"))
                    if tgt is None:
                        raise ValueError(f"attach.to 引用了不存在或尚未创建的部件 {at.get('to')!r}（部件需按依赖顺序排列）")
                    H.snap(o, tgt, side=at.get("side", "+Z"), overlap=at.get("overlap", 0.01),
                           offset=at.get("offset", (0, 0, 0)))
                else:
                    o.location = p.get("at", (0, 0, 0))
            built[name] = o
            arr = p.get("array")
            if isinstance(arr, dict) and int(arr.get("count", 1)) > 1:  # 等距重复（城垛、柱廊、窗列）
                bpy.context.view_layer.update()
                count = min(int(arr["count"]), 80)
                parent = built.get((p.get("attach") or {}).get("to")) if isinstance(p.get("attach"), dict) else None
                pb = H.bounds(parent) if parent is not None else None
                ob = H.bounds(o)
                along = str(arr.get("along", "")).upper()
                if along in ("X", "Y", "Z") and pb is not None:
                    # 沿父部件全长均匀排布 count 个（首尾贴齐父部件两端）
                    k = "XYZ".index(along)
                    lo = pb["min"][k] + ob["size"][k] / 2
                    hi = pb["max"][k] - ob["size"][k] / 2
                    step = (hi - lo) / (count - 1) if count > 1 else 0
                    base = list(o.location)
                    shift = lo - ob["center"][k]
                    base[k] += shift
                    o.location = base
                    positions = []
                    for i in range(1, count):
                        loc = list(base)
                        loc[k] += step * i
                        positions.append(loc)
                else:
                    off = Vector(arr.get("offset", (1, 0, 0)))
                    positions = [list(o.location + off * i) for i in range(1, count)]
                clipped = 0
                along_mode = along in ("X", "Y", "Z") and pb is not None
                for i, loc in enumerate(positions, start=1):
                    if pb is not None and not along_mode:  # 附着在父部件上时，中心超出父部件范围的副本裁掉（避免悬空）
                        c_center = [ob["center"][j] - o.location[j] + loc[j] for j in range(3)]
                        if any(c_center[j] < pb["min"][j] - 0.01 or c_center[j] > pb["max"][j] + 0.01
                               for j in range(3) if j != "XYZ".index((p["attach"].get("side") or "+Z")[1].upper())):
                            clipped += 1
                            continue
                    c = o.copy()
                    c.data = o.data  # 共享网格，省内存
                    c.name = f"{name}_{i}"
                    bpy.context.collection.objects.link(c)
                    c.location = loc
                    built[c.name] = c
                if clipped:
                    warnings.append(f"{name}: array 有 {clipped} 个副本超出 {p['attach']['to']} 范围，已裁掉")
            axis_m = str(p.get("mirror", "")).upper()
            if axis_m in ("X", "Y"):
                bpy.context.view_layer.update()
                k = 0 if axis_m == "X" else 1  # X：左右对称（x 取反）；Y：前后对称（y 取反）
                if p.get("shape") == "limb":  # 肢体：用镜像端点重新生成
                    q = dict(p, name=name + "_mirror")
                    q["from"] = [(-v if i == k else v) for i, v in enumerate(p["from"])]
                    q["to"] = [(-v if i == k else v) for i, v in enumerate(p["to"])]
                    m = _bp_make(q, mats[key])
                else:  # 其他：复制并镜像位置与旋转
                    m = o.copy()
                    m.data = o.data.copy()
                    m.name = name + "_mirror"
                    bpy.context.collection.objects.link(m)
                    loc = list(o.location)
                    loc[k] = -loc[k]
                    m.location = loc
                    r = o.rotation_euler
                    m.rotation_euler = (r.x, -r.y, -r.z) if k == 0 else (-r.x, r.y, -r.z)
                built[m.name] = m
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
    bpy.context.view_layer.update()
    boxes = {n: H.bounds(o) for n, o in built.items()}
    if boxes:  # 接触容差按场景尺寸缩放：小物件 1.5 cm，70 米的场景约 25 cm
        lo = [min(b["min"][i] for b in boxes.values()) for i in range(3)]
        hi = [max(b["max"][i] for b in boxes.values()) for i in range(3)]
        diag = sum((h - l) ** 2 for l, h in zip(lo, hi)) ** 0.5
    tol = max(0.015, 0.003 * diag) if boxes else 0.015
    floating = _floating_parts(built, boxes, tol)
    grounded = []
    if floating:  # 确定性修复：悬空部件沿 -Z 吸附到下方最近的表面（城垛、灌木、灯柱等）
        grounded = _gravity_snap(built, floating, boxes)
        if grounded:
            bpy.context.view_layer.update()
            boxes = {n: H.bounds(o) for n, o in built.items()}
            floating = _floating_parts(built, boxes, tol)
            warnings.append(f"{len(grounded)} 个悬空部件已自动落到下方表面")
    vol = lambda b: b["size"][0] * b["size"][1] * b["size"][2]
    total_vol = sum(vol(b) for b in boxes.values()) or 1.0
    floating_major = [n for n in floating if vol(boxes[n]) > 0.01 * total_vol]
    if floating:
        warnings.append(f"悬空部件（与其他部件没有接触）: {floating}")
    info = scene_info()
    return {"built": len(built), "errors": errors, "warnings": warnings, "floating": floating,
            "floating_major": floating_major, "auto_grounded": grounded,
            "parts": {n: {"size": b["size"], "center": b["center"]} for n, b in list(boxes.items())[:60]},
            "size": info["size"], "triangles": info["triangles"]}


def build_oskar(program, palette=None):
    """Oskar 式引擎：场景程序 → 角点状态 → 模块网格（纯 Python 包 genai_pipeline.oskar），此处只负责落到 Blender。"""
    import bmesh as _bm
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src not in sys.path:
        sys.path.insert(0, src)
    from genai_pipeline.oskar import build as OB
    from genai_pipeline.oskar import palette as OPAL
    from genai_pipeline.oskar import program as OP
    from genai_pipeline.oskar import tint as OT
    prop = program.get("prop") if isinstance(program, dict) else None
    if prop:
        # 物件构件（塔、房屋、码头、小船）：同一套落地流程（材质、圆角、描边），颜色全部来自实测
        from genai_pipeline.oskar import props as OPR
        try:
            built = OPR.build(prop)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        H.clear()
        st, checks = None, []
        out = {"parts": built["parts"]}
        pal = {**OPAL.resolve(None), **built["palette"]}
        tints = {}
    else:
        err = OP.validate_program(program)
        if err:
            return {"ok": False, "error": err}
        H.clear()
        st = OP.build_state(program)
        caps_rep = None
        if isinstance(program.get("capabilities"), dict):
            # 体素层能力（REACode 能力库）：扫描 → Router → 副本上执行 → verify.js → 不 fail 才写回角点状态
            try:
                from genai_pipeline.capabilities import voxel as CV
                caps_rep = CV.apply_to_state(st, program["capabilities"])
            except Exception as e:  # noqa: BLE001 — 能力出错不影响建场景
                caps_rep = {"status": "error", "why": f"{type(e).__name__}: {e}"[:400]}
        checks = OP.check_state(st)
        out = OB.build_mesh(st, seed=int(program.get("seed", 0)))
        pp = program.get("palette")
        pal = OPAL.resolve(pp, program.get("palette_base")) if isinstance(pp, dict) else OPAL.resolve(palette or pp)
        # 色彩可能性空间（x.com/OskSta 2023-10-11）：岩体 / 草地逐面在三角 / 渐变里取色，写进逐角颜色属性
        strength = float(program.get("tint", 1.0)) if isinstance(program.get("tint", 1.0), (int, float)) else 1.0
        tints = OT.part_colors(out["parts"], pal, seed=int(program.get("seed", 0)), level_h=st.level_h,
                               strength=strength, mode=str(program.get("tint_mode") or "oskar")) if strength > 0 else {}
    mats = {}

    def mat(role):
        if role not in mats:
            m = bpy.data.materials.get("osk_" + role) or bpy.data.materials.new("osk_" + role)
            m.use_nodes = True
            bsdf = m.node_tree.nodes.get("Principled BSDF")
            col = OPAL.hex_to_linear(pal.get(role, "#888888"))
            if bsdf:
                bsdf.inputs["Base Color"].default_value = (*col, 1)
                ca = m.node_tree.nodes.get("osk_col")
                if role in tints:   # 基色来自颜色属性（导出为 COLOR_0；Godot 导入后按顶点色着色）
                    if ca is None:
                        ca = m.node_tree.nodes.new("ShaderNodeVertexColor")
                        ca.name = "osk_col"
                        ca.layer_name = "Col"
                    m.node_tree.links.new(ca.outputs["Color"], bsdf.inputs["Base Color"])
                elif ca is not None:
                    m.node_tree.nodes.remove(ca)
                bsdf.inputs["Roughness"].default_value = 0.2 if role == "sea" else 0.85
                if role == "dome":
                    bsdf.inputs["Metallic"].default_value = 0.15
            m.diffuse_color = (*col, 1)
            mats[role] = m
        return mats[role]

    objs = []
    PROP_ROLES = ("shrub", "canopy", "cypress", "trunk", "foam", "water", "petal", "stem", "leaf", "center")   # 装饰：Godot 里不做碰撞
    SEA_ROLES = ("sea", "sea_deep", "ripple")   # 水面与岸外波纹：无碰撞、无描边
    for name, roles in (("oskar_scene", [r for r in out["parts"] if r not in SEA_ROLES + PROP_ROLES]),
                        ("oskar_props", [r for r in PROP_ROLES if r in out["parts"]]),
                        ("oskar_sea", list(SEA_ROLES))):
        roles = [r for r in roles if r in out["parts"]]
        if not roles:
            continue
        verts, faces, fmat, fcol = [], [], [], []
        for mi, r in enumerate(roles):
            part = out["parts"][r]
            base = len(verts)
            verts += part["v"]
            flat = (1.0, 1.0, 1.0)   # 不变化的角色写白色：glTF 导入端（Godot）按 基色 × 顶点色 着色，白色不改变基色
            cols = [OT.to_linear(c) for c in tints[r]] if r in tints else None
            for j, f in enumerate(part["f"]):
                faces.append([base + i for i in f])
                fmat.append(mi)
                fcol.append(cols[j] if cols else flat)
        me = bpy.data.meshes.new(name)
        me.from_pydata(verts, [], faces)
        if any(r in tints for r in roles):
            attr = me.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
            loop_cols = []
            for poly in me.polygons:
                c = fcol[poly.index]
                loop_cols += [c[0], c[1], c[2], 1.0] * poly.loop_total
            attr.data.foreach_set("color", loop_cols)
            me.color_attributes.active_color = attr
        for r in roles:
            me.materials.append(mat(r))
        for poly, mi in zip(me.polygons, fmat):
            poly.material_index = mi
        bm = _bm.new()
        bm.from_mesh(me)
        _bm.ops.remove_doubles(bm, verts=bm.verts, dist=0.003)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(ob)
        objs.append(ob)
        if name == "oskar_sea":
            ob["ignore_bounds"] = True
        bev = float(program.get("bevel", 0.12))
        if name == "oskar_scene" and bev > 0:   # 圆角：只倒尖锐棱（>30°），边缘不再刀切般尖；面数约 +2–3 倍（用户 2026-10-07）
            bm_ = ob.modifiers.new("round", "BEVEL")
            bm_.width = bev
            bm_.segments = 2
            bm_.limit_method = "ANGLE"
            bm_.angle_limit = math.radians(30)
            bm_.use_clamp_overlap = True
            if hasattr(bm_, "harden_normals"):
                bm_.harden_normals = False
        soft = float(program.get("soft_normals", 0.35))   # 0 关闭；实测 0.35 + 体素 0.8×层高：岩柱变圆润、桥与台阶仍干净（0.55 + 细体素会出斑驳）
        if name == "oskar_scene" and soft > 0:
            # 只柔岩体 / 地表：墙面、屋顶等建筑保持平整的平面受光（实测建筑也柔会出斑驳的墙）
            natural = [i for i, r in enumerate(roles) if r.startswith("rock") or r in ("grass", "ground", "sand", "earth",
                                                                                       "dirt", "cliff", "moss", "snow")]
            _voxel_normals(ob, voxel=float(program.get("voxel", 0.8 * float(program.get("level_h", 2.0)))), mix=soft,
                           only_materials=natural)
            me = ob.data   # 网格已换成烘焙后的那份
        if name in ("oskar_scene", "oskar_props"):  # 反转外壳描边（清晰剪影，x-timeline §2.4）
            om = bpy.data.materials.get("osk_outline") or bpy.data.materials.new("osk_outline")
            om.use_nodes = True
            b = om.node_tree.nodes.get("Principled BSDF")
            oc = OPAL.hex_to_linear(pal.get("outline", "#2b2b30"))
            if b:
                b.inputs["Base Color"].default_value = (*oc, 1)
                b.inputs["Roughness"].default_value = 1.0
            om.use_backface_culling = True
            if hasattr(om, "use_backface_culling_shadow"):   # 外壳不投自阴影（否则整件被罩在阴影里发暗）
                om.use_backface_culling_shadow = True
            me.materials.append(om)
            mod = ob.modifiers.new("outline", "SOLIDIFY")
            mod.thickness = float(program.get("outline", 0.07))
            mod.offset = 1.0
            mod.use_flip_normals = True
            mod.use_rim = False
            mod.material_offset = len(roles)
    info = scene_info()
    if prop:
        return {"ok": True, "checks": [], "notes": [], "parts": sorted(out["parts"]),
                "triangles": info.get("triangles"), "size": info.get("size")}
    return {"ok": True, "checks": checks, "notes": st.notes, "tiles": out["tiles"], "wfc": out["wfc"],
            "capabilities": caps_rep,
            "props": out["props"], "cells": out["cells"], "segments": out["segments"],
            "triangles": info.get("triangles"), "size": info.get("size"), "walk": out.get("walk"), "paths": out.get("paths")}


def _voxel_normals(ob, voxel=0.7, mix=0.55, iterations=20, only_materials=None):
    """Oskar《Bad North》的“体素法线”（2018-11 推文：用体素法线给岛再平滑一层，打散网格感，更自然）：
    复制一份地形 → 体素重建 + 平滑得到一个圆润的“包络”，把它的法线按 mix 混回原网格的自定义法线。
    形体（剪影、台阶、面数）不变，只是受光变柔：棱边不再刀切般尖，台阶仍清楚。先烘焙进网格（含圆角），再删代理。"""
    if only_materials is not None:
        if not only_materials:
            return
        keep = set(only_materials)
        vs = sorted({v for p in ob.data.polygons if p.material_index in keep for v in p.vertices})
        vg = ob.vertex_groups.new(name="soft")
        vg.add(vs, 1.0, "REPLACE")
    dg = bpy.context.evaluated_depsgraph_get()
    base = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)   # 含圆角等
    proxy = bpy.data.objects.new(ob.name + "_vox", base.copy())
    bpy.context.scene.collection.objects.link(proxy)
    proxy.matrix_world = ob.matrix_world
    rm = proxy.modifiers.new("vox", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = max(0.05, voxel)
    sm = proxy.modifiers.new("smooth", "SMOOTH")
    sm.factor = 0.8
    sm.iterations = iterations
    for m in list(ob.modifiers):
        ob.modifiers.remove(m)
    old = ob.data
    ob.data = base
    if old.users == 0:
        bpy.data.meshes.remove(old)
    dt = ob.modifiers.new("vox_normals", "DATA_TRANSFER")
    dt.object = proxy
    dt.use_loop_data = True
    dt.data_types_loops = {"CUSTOM_NORMAL"}
    dt.loop_mapping = "POLYINTERP_NEAREST"
    dt.mix_mode = "REPLACE"
    dt.mix_factor = max(0.0, min(1.0, mix))
    if only_materials is not None:
        dt.vertex_group = "soft"
    dg = bpy.context.evaluated_depsgraph_get()
    baked = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    ob.modifiers.remove(dt)
    tmp = ob.data
    ob.data = baked
    bpy.data.meshes.remove(tmp)
    pm = proxy.data
    bpy.data.objects.remove(proxy)
    bpy.data.meshes.remove(pm)


def compose_scene(items):
    """场景组装：按布局把子资产 GLB 放进场景。items=[{"id","glb","at":[x,y,z],"rot_z":度,"scale":s}]。
    每个实例是一个空物体（名 = id）下挂导入的网格，便于整体移动/缩放。"""
    H.clear()
    placed, errors = [], []
    for it in items:
        try:
            before = set(bpy.data.objects.keys())
            bpy.ops.import_scene.gltf(filepath=it["glb"])
            new = [bpy.data.objects[n] for n in set(bpy.data.objects.keys()) - before]
            root = H.empty(it["id"])
            for o in new:
                if o.parent is None:
                    o.parent = root
                if it.get("backdrop"):
                    o["ignore_bounds"] = True   # 远景层不参与取景，主体才能填满画面
            root.location = _vec3(it.get("at"), [0, 0, 0])
            root.rotation_euler = (0, 0, math.radians(_num(it.get("rot_z"), 0)))
            s = _num(it.get("scale"), 1.0) or 1.0
            root.scale = (s, s, s)
            placed.append(it["id"])
        except Exception as e:
            errors.append(f"{it.get('id')}: {type(e).__name__}: {e}")
    bpy.context.view_layer.update()
    grounded = _ground_on_terrain(items)
    boxes = {}
    for it in items:
        o = bpy.data.objects.get(it["id"])
        if o is not None:
            b = H.bounds(o)
            boxes[it["id"]] = {"size": b["size"], "center": b["center"], "min": b["min"], "max": b["max"]}
    info = scene_info()
    return {"placed": placed, "errors": errors, "instances": boxes, "size": info["size"],
            "triangles": info["triangles"], "grounded": grounded}


def _ground_on_terrain(items):
    """落地：标了 ground 的实例（建筑、道具、角色）竖直落到 terrain 实例（岛体 / 地面）的表面上——
    在实例占地范围内取 9 个点向下射线，取最高命中（不陷进坡里）；脚下没有地形（海上的船、码头）就不动。
    拆分给的摆放高度常是 0，而岛顶在十几米高处，不落地就会悬在半空。"""
    from mathutils.bvhtree import BVHTree
    trees = []
    for it in items:
        if not it.get("terrain"):
            continue
        root = bpy.data.objects.get(it["id"])
        if root is None:
            continue
        for o in [root, *root.children_recursive]:
            if o.type == "MESH" and o.data.polygons:
                mw = o.matrix_world
                verts = [mw @ v.co for v in o.data.vertices]
                trees.append(BVHTree.FromPolygons(verts, [tuple(p.vertices) for p in o.data.polygons]))
    out = {}
    if not trees:
        return out
    down = Vector((0, 0, -1))
    for it in items:
        if not it.get("ground") or it.get("terrain") or it.get("backdrop"):
            continue
        root = bpy.data.objects.get(it["id"])
        if root is None:
            continue
        b = H.bounds(root)
        (x0, y0, z0), (x1, y1, _) = b["min"], b["max"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hits = []
        for fx in (-0.35, 0.0, 0.35):
            for fy in (-0.35, 0.0, 0.35):
                p = Vector((cx + (x1 - x0) * fx, cy + (y1 - y0) * fy, 1e4))
                for t in trees:
                    loc, _, _, _ = t.ray_cast(p, down)
                    if loc is not None:
                        hits.append(loc.z)
        if len(hits) < 3:   # 大半个占地都不在地形上：留在原处（海上 / 水边）
            continue
        dz = max(hits) - z0
        root.location.z += dz
        out[it["id"]] = round(dz, 3)
    bpy.context.view_layer.update()
    return out


def compose_planet(items, radius=0.0, gap=10.0, links=None, ocean_color=None, tilt=18.0):
    """把多个平面场景 GLB 贴到同一颗小行星上（方位等距投影，见 genai_pipeline.oskar.planet）：
    items=[{"id","glb","lat","lon","yaw","r"}]（缺 lat/lon 时按旅程带自动排布），radius=海平面半径（0 = 自动）；
    去掉各场景自带的方形海盘，换成一整颗海洋球（保留岸外波纹），items 里也可以是地势连接（kind="link"，由 Oskar 引擎生成，同样按自己的经纬度 / 朝向贴上）；
    links 非空时才生成旧版陆桥（atlas 整合传 links=[]：场景之间靠地势连接，不搭桥）。"""
    import bmesh as _bm
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src not in sys.path:
        sys.path.insert(0, src)
    from genai_pipeline.oskar import planet as PL
    from genai_pipeline.oskar.palette import hex_to_linear as OPAL_hex
    H.clear()
    root = H.empty("oskar_planet")
    errors, placed, sizes, raw = [], [], {}, {}
    # 1. 导入并把变换写进网格（平面场景坐标：海平面 z=0）
    for it in items:
        try:
            before = set(bpy.data.objects.keys())
            bpy.ops.import_scene.gltf(filepath=it["glb"])
            new = [bpy.data.objects[n] for n in set(bpy.data.objects.keys()) - before]
            meshes = []
            for o in new:
                if o.type == "MESH":
                    mw = o.matrix_world.copy()
                    o.parent = None
                    o.data = o.data.copy()
                    o.data.transform(mw)
                    o.matrix_world = Matrix.Identity(4)
                    meshes.append(o)
            for o in new:
                if o.type != "MESH":
                    bpy.data.objects.remove(o, do_unlink=True)
            xs = [v.co for o in meshes for v in o.data.vertices]
            mn = [min(c[i] for c in xs) for i in range(3)] if xs else [0, 0, 0]
            mx = [max(c[i] for c in xs) for i in range(3)] if xs else [0, 0, 0]
            sizes[it["id"]] = [mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]]
            raw[it["id"]] = meshes
        except Exception as e:
            errors.append(f"{it.get('id')}: {type(e).__name__}: {e}")
    ids = [it["id"] for it in items if it["id"] in raw]
    if not ids:
        return {"ok": False, "errors": errors or ["没有可用的场景"]}
    # 2. 排布：给定经纬度则沿用，否则按旅程带自动排
    if all("lat" in it and "lon" in it for it in items) and radius:
        lay = {"radius": float(radius), "scenes": [{"id": it["id"], "lat": float(it["lat"]), "lon": float(it["lon"]),
                                                     "yaw": float(it.get("yaw", 0)), "r": float(it.get("r") or PL.island_radius(sizes[it["id"]]))}
                                                    for it in items if it["id"] in raw]}
        lay["links"] = links if links is not None else [[ids[i], ids[i + 1]] for i in range(len(ids) - 1)]
    else:
        lay = PL.plan(ids, sizes, gap=gap, tilt=tilt)
        if links is not None:
            lay["links"] = links
    R = lay["radius"]
    by_id = {s["id"]: s for s in lay["scenes"]}
    # 3. 贴到球面：删去海盘与水体侧壁（保留波纹），顶点按方位等距投影弯曲
    for sid in ids:
        s = by_id[sid]
        fr = PL.frame(s["lat"], s["lon"], s.get("yaw", 0.0))
        holder = H.empty(sid)
        holder.parent = root
        for o in raw[sid]:
            me = o.data
            drop = {i for i, m in enumerate(me.materials) if m and m.name.split(".")[0] in ("osk_sea", "osk_sea_deep")}
            if drop:
                bm = _bm.new()
                bm.from_mesh(me)
                _bm.ops.delete(bm, geom=[f for f in bm.faces if f.material_index in drop], context="FACES")
                bm.to_mesh(me)
                bm.free()
            if not me.polygons:
                bpy.data.objects.remove(o, do_unlink=True)
                continue
            for v in me.vertices:
                v.co = PL.to_sphere((v.co.x, v.co.y, v.co.z), fr, R)
            me.update()
            base = o.name.split(".")[0]
            o.name = f"{base}_{sid}"
            o.parent = holder
        placed.append(sid)
    # 4. 海洋球
    bpy.ops.mesh.primitive_uv_sphere_add(segments=96, ring_count=48, radius=R, location=(0, 0, 0))
    ocean = bpy.context.active_object
    ocean.name = "oskar_ocean"
    bpy.ops.object.shade_smooth()
    ocean_mat = bpy.data.materials.new("osk_ocean")        # 海洋球：哑光（不要高光点），颜色取场景的海色
    src_sea = bpy.data.materials.get("osk_sea")
    col = (0.18, 0.55, 0.68, 1)
    if src_sea is not None and src_sea.use_nodes and src_sea.node_tree.nodes.get("Principled BSDF"):
        col = tuple(src_sea.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value)
    elif ocean_color:
        col = (*OPAL_hex(ocean_color), 1)
    ocean_mat.use_nodes = True
    b = ocean_mat.node_tree.nodes.get("Principled BSDF")
    if b:
        b.inputs["Base Color"].default_value = col
        b.inputs["Roughness"].default_value = 0.85
    ocean_mat.diffuse_color = col
    ocean.data.materials.append(ocean_mat)
    ocean.parent = root
    # 5. 旧版陆桥（仅 links 非空时）
    grass = bpy.data.materials.get("osk_grass") or H.mat("osk_grass", "#6F8C48")
    rock = bpy.data.materials.get("osk_rock") or H.mat("osk_rock", "#A2A6A5")
    path = bpy.data.materials.get("osk_stone_alt") or H.mat("osk_stone_alt", "#E2D8C0")
    verts, faces, fmat = [], [], []
    for a, b in lay["links"]:
        if a not in by_id or b not in by_id:
            continue
        rb = PL.ribbon(PL.link_path(by_id[a], by_id[b], R), R)
        for role, mi in (("grass", 0), ("path", 1), ("rock", 2)):
            for q in rb[role]:
                base = len(verts)
                verts += [tuple(p) for p in q]
                faces.append([base, base + 1, base + 2, base + 3])
                fmat.append(mi)
    if faces:
        me = bpy.data.meshes.new("oskar_links")
        me.from_pydata(verts, [], faces)
        for m in (grass, path, rock):
            me.materials.append(m)
        for poly, mi in zip(me.polygons, fmat):
            poly.material_index = mi
        bm = _bm.new()
        bm.from_mesh(me)
        _bm.ops.remove_doubles(bm, verts=bm.verts, dist=0.01)
        _bm.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
        lk = bpy.data.objects.new("oskar_links", me)
        bpy.context.scene.collection.objects.link(lk)
        lk.parent = root
        om = bpy.data.materials.get("osk_outline")
        if om:
            me.materials.append(om)
            mod = lk.modifiers.new("outline", "SOLIDIFY")
            mod.thickness = 0.07
            mod.offset = 1.0
            mod.use_flip_normals = True
            mod.use_rim = False
            mod.material_offset = 3
    bpy.context.view_layer.update()
    info = scene_info()
    return {"ok": not errors, "errors": errors, "placed": placed, "radius": R, "scenes": lay["scenes"],
            "links": lay["links"], "sizes": sizes, "size": info["size"], "triangles": info["triangles"]}


def oskar_look(outline=0.0, flat=True):
    """Oskar 统一外观（给直接建模的单个资产用）：平面着色 + 反转外壳描边（厚度按物体尺寸自适应），
    与 Oskar 场景同款，导出时修改器自动应用。透明材质的部件不加描边。"""
    objs = _mesh_objects()
    mn, mx = _bounds()
    size = max(mx - mn) if mn is not None else 1.0
    th = float(outline) or max(0.008, min(0.07, size * 0.012))
    om = bpy.data.materials.get("osk_outline") or bpy.data.materials.new("osk_outline")
    om.use_nodes = True
    b = om.node_tree.nodes.get("Principled BSDF")
    if b:
        b.inputs["Base Color"].default_value = (0.024, 0.023, 0.028, 1)
        b.inputs["Roughness"].default_value = 1.0
    om.use_backface_culling = True
    if hasattr(om, "use_backface_culling_shadow"):   # 外壳不投自阴影（否则整件被罩在阴影里发暗）
        om.use_backface_culling_shadow = True
    import bmesh as _bm
    done, n = set(), 0
    for o in objs:
        # 缩放写进网格（描边厚度才均匀）+ 法线统一朝外（否则外壳翻到正面，整件变暗）
        if o.data.users > 1:
            o.data = o.data.copy()
        if any(abs(c - 1.0) > 1e-4 for c in o.scale) and not o.modifiers:
            o.data.transform(Matrix.Diagonal((*o.scale, 1.0)))
            o.scale = (1.0, 1.0, 1.0)
        bm = _bm.new()
        bm.from_mesh(o.data)
        _bm.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(o.data)
        bm.free()
        me = o.data
        if flat:
            for p in me.polygons:
                p.use_smooth = False
        if any(m.type == "SOLIDIFY" and m.name == "outline" for m in o.modifiers):
            continue

        def alpha(m):
            bs = m.node_tree.nodes.get("Principled BSDF") if m and m.use_nodes else None
            return bs.inputs["Alpha"].default_value if bs and "Alpha" in bs.inputs else 1.0
        if any(alpha(m) < 0.99 for m in me.materials if m):
            continue
        if me.name not in done:
            if not me.materials:
                me.materials.append(bpy.data.materials.get("osk_default") or bpy.data.materials.new("osk_default"))
            if om.name not in [m.name for m in me.materials if m]:
                me.materials.append(om)
            done.add(me.name)
        mod = o.modifiers.new("outline", "SOLIDIFY")
        mod.thickness = th
        mod.offset = 1.0
        mod.use_flip_normals = True
        mod.use_rim = False
        mod.material_offset = [m.name if m else "" for m in me.materials].index(om.name)
        n += 1
    return {"objects": len(objs), "outlined": n, "outline": round(th, 4)}


def _helper_objects():
    return [o for o in bpy.data.objects if o.name.startswith("_pipe_")]


def export_glb(path):
    info = scene_info()
    problems = []
    if info["nonfinite_transforms"]:
        problems.append(f"非有限变换: {info['nonfinite_transforms']}")
    if info["triangles"] == 0:
        problems.append("没有网格三角面")
    if problems:
        return {"ok": False, "problems": problems, "info": info}
    hidden = []
    for o in _helper_objects():
        hidden.append((o, o.hide_get()))
        o.hide_set(True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.context.scene.objects:
        if not o.name.startswith("_pipe_"):
            o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True,
                              export_yup=True)
    for o, h in hidden:
        o.hide_set(h)
    return {"ok": True, "path": path, "bytes": os.path.getsize(path), "info": info}


def save_blend(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    return {"path": path}


def open_blend(path):
    bpy.ops.wm.open_mainfile(filepath=path)
    return scene_info()


def import_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path) as (src, dst):
            dst.objects = src.objects
        for o in dst.objects:
            if o is not None:
                bpy.context.collection.objects.link(o)
    else:
        raise ValueError(f"不支持的格式 {ext}")
    return scene_info()


def _euler(*a, **kw):
    """宽容版 Euler：同时接受 Euler(x, y, z) 与 Euler((x, y, z), 'XYZ')。"""
    if len(a) == 3 and all(isinstance(v, (int, float)) for v in a):
        return mathutils.Euler(a, kw.get("order", "XYZ"))
    return mathutils.Euler(*a, **kw)


NS = {"bpy": bpy, "bmesh": bmesh, "math": math, "mathutils": mathutils, "Vector": Vector, "Matrix": Matrix,
      "Euler": _euler, "H": H}

HINTS = [
    ("has no attribute 'material'", "对象没有 material 属性：用 H.assign(obj, mat) 赋材质，或 obj.data.materials.append(mat)"),
    ("has no attribute 'materials'", "材质列表在网格数据上：obj.data.materials；推荐 H.assign(obj, mat)"),
    ("has no attribute 'rotation'", "对象旋转属性是 obj.rotation_euler（弧度）；H 助手的 rotation=(x,y,z) 参数单位是度"),
    ("has no attribute 'scale_to'", "缩放用 obj.scale = (x, y, z)"),
    ("unexpected keyword argument", "H 助手参数见工具说明；未知参数会被忽略，请检查拼写"),
    ("context is incorrect", "bpy.ops 需要正确上下文：先 H.select_only([obj]) 再调用，或改用数据 API / H 助手"),
    ("cannot delete function call", "del 只能删除变量；删除对象用 H.delete(name_or_obj)"),
]


def _snapshot():
    return {k: set(getattr(bpy.data, k).keys()) for k in ("objects", "meshes", "materials", "curves")}


def _rollback(before):
    removed = []
    for k in ("objects", "meshes", "curves", "materials"):
        coll = getattr(bpy.data, k)
        for name in set(coll.keys()) - before[k]:
            d = coll.get(name)
            if d is not None:
                if k == "objects":
                    removed.append(name)
                coll.remove(d)
    return removed


def op_exec(code):
    buf = io.StringIO()
    NS.pop("result", None)
    before = _snapshot()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<agent>", "exec"), NS)
    except Exception as e:
        # 原子性：回滚本次调用新建的数据，模型修正后可直接重跑整段代码，不会产生 .001 重复对象
        removed = _rollback(before)
        msg = f"{type(e).__name__}: {e}"
        hints = [h for pat, h in HINTS if pat in msg]
        raise RuntimeError(msg + (f"\n[已回滚本次新建的 {len(removed)} 个对象，修正后重新执行整段代码即可；不要 reset 场景]"
                                  if removed else "\n[本次调用未新建对象]")
                           + ("\n提示：" + "；".join(hints) if hints else "")
                           + (f"\nprint 输出：{buf.getvalue()[-1500:]}" if buf.getvalue() else "")) from e
    return {"stdout": buf.getvalue()[-8000:], "result": _jsonable(NS.get("result"))}


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except TypeError:
        return repr(v)[:2000]


def _code_hash():
    import glob
    import hashlib
    h = hashlib.sha1(open(__file__, "rb").read())
    here = os.path.dirname(os.path.abspath(__file__))
    for f in [os.path.join(here, "look.py")] + sorted(glob.glob(os.path.join(os.path.dirname(here), "oskar", "*.py"))):
        h.update(open(f, "rb").read())
    return h.hexdigest()[:12]


CODE_HASH = _code_hash()

def _load_mask_image(path, n=96):
    """读入图像为 numpy (h, w, 4) 0–1（Blender 自带 numpy，无 PIL）。"""
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(h, w, 4)[::-1]          # Blender 像素自下而上存储，翻成自上而下


def _srgb_to_linear(c):
    import numpy as np
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def refine_generated(path, size_m, budget=8000, image=None, mask=None, colors=6, name="asset", palette=None, bands=None):
    """整理图生 3D 的形体网格，使其成为可用的 Oskar 风格游戏资产：
    1) 导入、合并为一个物体；2) 按目标图剪影在四个朝向里选最像的正面（朝 -Y）；3) 等比缩放到 size_m 的最大边；
    4) 超预算则精简面数；5) 从目标图正面投影取色，k-means 量化成少量色块（逐面平涂，每色一个材质）；6) 落地居中。"""
    import numpy as np
    H.clear()
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=path)
    new = [bpy.data.objects[n] for n in set(bpy.data.objects.keys()) - before]
    meshes = [o for o in new if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("生成结果里没有网格")
    for o in new:  # 解除父子关系并保留世界变换，便于合并
        if o.type == "MESH" and o.parent is not None:
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw
    H.select_only(meshes)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = name
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for o in [x for x in new if x.name in bpy.data.objects and x.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    me = ob.data
    n = len(me.vertices)
    V = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("co", V)
    V = V.reshape(n, 3)
    me.calc_loop_triangles()
    T = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", T)
    T = T.reshape(-1, 3)

    # ── 目标图与掩码 ──
    img = _load_mask_image(image) if image else None
    msk = None
    if mask:
        m = _load_mask_image(mask)
        msk = m[..., 0] > 0.5
    elif img is not None and img.shape[2] == 4 and img[..., 3].min() < 0.98:
        msk = img[..., 3] > 0.5
    if msk is None and img is not None:   # 没有掩码：与四角背景色差异大的像素当作物体
        corners = np.stack([img[0, 0, :3], img[0, -1, :3], img[-1, 0, :3], img[-1, -1, :3]])
        msk = np.abs(img[..., :3] - np.median(corners, axis=0)).sum(-1) > 0.1
    report = {"faces_in": int(len(T))}

    # ── 2) 朝向：表面撒点，四个偏航角下投影到 XZ（正视方向 +Y），与目标剪影比 IoU ──
    rng = np.random.default_rng(0)
    pick = rng.integers(0, len(T), min(len(T), 40000))
    r1, r2 = rng.random((2, len(pick)))
    s1 = np.sqrt(r1)
    a, b, c = V[T[pick, 0]], V[T[pick, 1]], V[T[pick, 2]]
    P = (1 - s1)[:, None] * a + (s1 * (1 - r2))[:, None] * b + (s1 * r2)[:, None] * c

    def silhouette(pts, G=64):
        u, v = pts[:, 0], pts[:, 2]
        span = max(u.max() - u.min(), v.max() - v.min(), 1e-6)
        gu = ((u - u.min()) / span * (G - 1)).astype(int)
        gv = ((v.max() - v) / span * (G - 1)).astype(int)
        grid = np.zeros((G, G), bool)
        grid[gv, gu] = True
        grid = grid | np.roll(grid, 1, 0) | np.roll(grid, 1, 1)
        return grid, (u.min(), v.max(), span)

    def mask_grid(G=64):
        ys, xs = np.nonzero(msk)
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        span = max(x1 - x0, y1 - y0, 1)
        grid = np.zeros((G, G), bool)
        grid[((ys - y0) / span * (G - 1)).astype(int), ((xs - x0) / span * (G - 1)).astype(int)] = True
        return grid, (x0, y0, span)

    best = (0, -1.0, False)
    if msk is not None and msk.any():
        mg, _ = mask_grid()
        for yaw in (0, 90, 180, 270):
            t = math.radians(yaw)
            R = np.array([[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1]])
            for mirror in (False, True):
                Q = P @ R.T
                if mirror:
                    Q = Q * np.array([-1, 1, 1])
                g, _ = silhouette(Q)
                iou = float((g & mg).sum()) / float(max(1, (g | mg).sum()))
                if iou > best[1] + 1e-6:
                    best = (yaw, iou, mirror)
    yaw, iou, mirror = best
    t = math.radians(yaw)
    R = np.array([[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1]])
    V = V @ R.T
    if mirror:   # 镜像会翻转绕序：同时翻转三角形顶点顺序（通过网格 flip 实现）
        V = V * np.array([-1, 1, 1])
    report.update(yaw=yaw, mirror=mirror, silhouette_iou=round(max(iou, 0), 3))

    # ── 3) 缩放到目标尺寸（最大边对齐，保持比例） ──
    dims = V.max(0) - V.min(0)
    tgt = max(float(x) for x in size_m) if size_m else float(dims.max())
    k = tgt / max(float(dims.max()), 1e-6)
    V = V * k
    V -= np.array([(V[:, 0].max() + V[:, 0].min()) / 2, (V[:, 1].max() + V[:, 1].min()) / 2, V[:, 2].min()])
    me.vertices.foreach_set("co", V.astype(np.float32).ravel())
    if mirror:
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.reverse_faces(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
    me.update()

    # ── 4) 精简到预算 ──
    tris = sum(len(p.vertices) - 2 for p in me.polygons)
    if budget and tris > budget:
        mod = ob.modifiers.new("decimate", "DECIMATE")
        mod.ratio = max(0.02, float(budget) / tris)
        bpy.context.view_layer.objects.active = ob
        bpy.ops.object.modifier_apply(modifier=mod.name)
    me = ob.data

    if bands:
        # 色带物件：先在每条色带的分界高度把网格切开，色带边缘才是水平直线（不切的话长三角跨过分界，条纹成锯齿）
        import bmesh as _bm
        zs = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", zs)
        zs = zs.reshape(-1, 3)[:, 2]
        z0_, z1_ = float(zs.min()), float(zs.max())
        bm = _bm.new()
        bm.from_mesh(me)
        for b in bands[1:]:
            zc_ = z1_ - float(b["from"]) * (z1_ - z0_)
            geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
            _bm.ops.bisect_plane(bm, geom=geom, plane_co=(0, 0, zc_), plane_no=(0, 0, 1))
        bm.to_mesh(me)
        bm.free()
        me.update()

    # ── 5) 正面投影取色 + 色块量化 ──
    for mt in list(me.materials):
        me.materials.pop()
    nf = len(me.polygons)
    centers = np.empty(nf * 3)
    me.polygons.foreach_get("center", centers)
    centers = centers.reshape(nf, 3)
    if img is not None and msk is not None and msk.any():
        ys, xs = np.nonzero(msk)
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        Vn = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", Vn)
        Vn = Vn.reshape(-1, 3)
        u0, u1, v0, v1 = Vn[:, 0].min(), Vn[:, 0].max(), Vn[:, 2].min(), Vn[:, 2].max()
        px = x0 + (centers[:, 0] - u0) / max(u1 - u0, 1e-6) * (x1 - x0)
        py = y0 + (v1 - centers[:, 2]) / max(v1 - v0, 1e-6) * (y1 - y0)
        px = np.clip(px.round().astype(int), 0, msk.shape[1] - 1)
        py = np.clip(py.round().astype(int), 0, msk.shape[0] - 1)
        # 掩码外（背景、被遮挡的空洞）用最近的物体颜色向外扩散填满，投影取色时不会取到背景灰
        fill = img[..., :3].copy()
        known = msk.copy()
        for _ in range(256):
            if known.all():
                break
            acc = np.zeros_like(fill)
            cnt = np.zeros(known.shape, dtype=np.float32)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                k2 = np.roll(known, (dy, dx), axis=(0, 1))
                acc += np.roll(fill, (dy, dx), axis=(0, 1)) * k2[..., None]
                cnt += k2
            grow = (~known) & (cnt > 0)
            fill[grow] = acc[grow] / cnt[grow][:, None]
            known = known | grow
        col = fill[py, px]
    else:
        col = np.tile(np.array([[0.62, 0.6, 0.56]]), (nf, 1))
    hexrgb = lambda h: [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]   # noqa: E731
    if bands:
        # 配色环节测得的自上而下色带（细高物件，如灯塔条纹）：按面中心高度直接取带色，不再正面投影
        zc = centers[:, 2]
        z0, z1 = float(zc.min()), float(zc.max())
        t = (z1 - zc) / max(z1 - z0, 1e-6)
        cent = np.array([hexrgb(b["hex"]) for b in bands])
        lab = np.zeros(nf, dtype=int)
        for j, b in enumerate(bands):
            lab[(t >= float(b["from"]) - 1e-6) & (t <= float(b["to"]) + 1e-6)] = j
        report["coloring"] = "bands"
    elif palette:
        # 配色环节测得的色块：投影取到的颜色吸附到最近的实测色（去掉投影边缘的脏色和锯齿）
        cent = np.array([hexrgb(h) for h in palette])
        lab = np.argmin(((col[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)
        report["coloring"] = "palette"
    else:
        K = max(1, min(int(colors), len(col)))
        cent = col[np.linspace(0, len(col) - 1, K).astype(int)].copy()
        for _ in range(12):
            lab = np.argmin(((col[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)
            for j in range(K):
                if (lab == j).any():
                    cent[j] = col[lab == j].mean(0)
    K = len(cent)
    lin = _srgb_to_linear(cent)
    for j in range(K):
        mt = bpy.data.materials.new(f"{name}_c{j}")
        mt.use_nodes = True
        bsdf = mt.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (*lin[j], 1.0)
            bsdf.inputs["Roughness"].default_value = 0.85
        me.materials.append(mt)
    me.polygons.foreach_set("material_index", lab.astype(np.int32))
    me.polygons.foreach_set("use_smooth", np.zeros(nf, dtype=bool))
    me.update()
    H.ground_to_origin()
    info = scene_info()
    report.update(faces_out=info["triangles"], size=info["size"], colors=[
        "#%02x%02x%02x" % tuple(int(max(0, min(1, x)) * 255) for x in cc) for cc in cent])
    return report


def fit_to_size(size_m, name=None):
    """把当前场景所有网格等比缩放到目标尺寸（水平最大边对齐 size_m 的水平最大边；水平尺寸很小时按高度），落地居中。"""
    mn, mx = _bounds()
    if mn is None:
        raise RuntimeError("场景中没有网格")
    dims = mx - mn
    horiz = max(dims.x, dims.y)
    want_h = max(float(size_m[0]), float(size_m[1]))
    k = want_h / horiz if horiz > 1e-6 and want_h > 0 else float(size_m[2]) / max(dims.z, 1e-6)
    for o in bpy.data.objects:
        if o.parent is None and not o.name.startswith("_pipe_"):
            o.location = o.location * k
            o.scale = o.scale * k
    bpy.context.view_layer.update()
    H.ground_to_origin()
    info = scene_info()
    return {"scale": round(k, 4), "size": info["size"], "triangles": info["triangles"]}


def build_backdrop(size_m, image=None, mask=None, name="backdrop", seed=0, colors=4, flat=False):
    """远景层（云海、海面、远处地平线）：在 size_m 的范围里铺一片起伏的团块（压扁的低模球），
    颜色按深度从参考裁剪图的上（远）到下（近）取样，再量化成少量色块。整片标记 ignore_bounds，取景时不撑大画面。"""
    import numpy as np
    import random as _r
    H.clear()
    rng = _r.Random(int(seed))
    sx, sy, sz = (max(0.5, float(v)) for v in size_m)
    img = _load_mask_image(image) if image else None
    msk = (_load_mask_image(mask)[..., 0] > 0.5) if mask else None
    if img is not None and msk is not None and msk.shape != img.shape[:2]:   # 掩码与图分辨率不同（分析分辨率）：最近邻对齐
        hh, ww = img.shape[:2]
        msk = msk[(np.arange(hh) * msk.shape[0] // hh)][:, (np.arange(ww) * msk.shape[1] // ww)]
    def band_color(t):   # t: 0 = 最远（图上方）… 1 = 最近（图下方）
        if img is None:
            return np.array([0.85, 0.82, 0.9])
        h, w = img.shape[:2]
        top, span = 0, h
        if msk is not None and msk.any():   # 只在掩码覆盖的行段里从远到近取色（整幅图 + 水面掩码时，t=0 是海平线）
            rows = np.nonzero(msk.any(1))[0]
            top, span = int(rows.min()), int(rows.max() - rows.min() + 1)
        y0 = top + int(t * (span - 1) * 0.9)
        y1 = min(h, y0 + max(2, span // 10))
        band = img[y0:y1, :, :3]
        if msk is not None and msk[y0:y1].any():
            band = band[msk[y0:y1]]
        else:
            band = band.reshape(-1, 3)
        return band.mean(0) if len(band) else np.array([0.85, 0.82, 0.9])
    if flat:
        # 海面 / 地面（深度定位实测躺在支撑面上）：平的一块板，顶面按远近分条上色（海平线浅、近处深），
        # 不做云团那样的起伏团块；板厚 = size_m 的高，摆放时顶面正好压在 z=0
        strips = 24
        me = bpy.data.meshes.new(name)
        verts, faces, mats_idx, cols_ = [], [], [], []
        for i in range(strips):
            y0, y1 = -sy / 2 + sy * i / strips, -sy / 2 + sy * (i + 1) / strips
            t = 1.0 - (i + 0.5) / strips   # 近处（y 小）t 大，取掩码下方的行
            b = len(verts)
            verts += [(-sx / 2, y0, sz), (sx / 2, y0, sz), (sx / 2, y1, sz), (-sx / 2, y1, sz)]
            faces.append([b, b + 1, b + 2, b + 3])
            cols_.append(band_color(max(0.0, min(1.0, t))))
        b = len(verts)
        verts += [(-sx / 2, -sy / 2, 0), (-sx / 2, sy / 2, 0), (sx / 2, sy / 2, 0), (sx / 2, -sy / 2, 0)]
        faces.append([b, b + 1, b + 2, b + 3])
        cols_.append(cols_[0])
        me.from_pydata(verts, [], faces)
        lin = _srgb_to_linear(np.clip(np.array(cols_), 0, 1))
        for j, poly in enumerate(me.polygons):
            mt = bpy.data.materials.new(f"{name}_s{j}")
            mt.use_nodes = True
            bsdf = mt.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                bsdf.inputs["Base Color"].default_value = (*lin[j], 1.0)
                # 不要高光：低机位下平板海面会整片反射天光发白（实测近处的深蓝渲成了浅青）
                bsdf.inputs["Roughness"].default_value = 1.0
                for key in ("Specular IOR Level", "Specular"):
                    if key in bsdf.inputs:
                        bsdf.inputs[key].default_value = 0.0
            me.materials.append(mt)
            poly.material_index = j
        ob = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(ob)
        ob["ignore_bounds"] = True
        H.ground_to_origin()
        info = scene_info()
        return {"puffs": 0, "flat": True, "size": info["size"], "triangles": info["triangles"],
                "colors": ["#%02x%02x%02x" % tuple(int(max(0, min(1, x)) * 255) for x in c) for c in cols_[:-1:6]]}
    r = max(sx, sy) / 18.0
    n = int(min(600, max(60, (sx * sy) / (r * r) * 1.6)))
    bm = bmesh.new()
    cols = []
    for i in range(n):
        x = rng.uniform(-sx / 2, sx / 2)
        y = rng.uniform(-sy / 2, sy / 2)
        rr = r * rng.uniform(0.6, 1.4)
        z = rng.uniform(0, sz * 0.4)
        flat = min(rng.uniform(0.35, 0.6), sz * 0.3 / rr)   # 团块厚度受 size_m 的高度约束，整片不高过 sz
        geom = bmesh.ops.create_icosphere(bm, subdivisions=1, radius=rr)
        verts = geom["verts"]
        for v in verts:
            v.co.x = v.co.x * rng.uniform(1.1, 1.6) + x
            v.co.y = v.co.y * rng.uniform(1.1, 1.6) + y
            v.co.z = v.co.z * flat + z
        t = 1.0 - (y + sy / 2) / sy      # y 越大越远 → 取图的上方
        c = band_color(max(0.0, min(1.0, t))) * rng.uniform(0.92, 1.06)
        faces = {f for v in verts for f in v.link_faces}
        cols.append((faces, c))
    me = bpy.data.meshes.new(name)
    # 先给每个面记录颜色，再量化成 colors 个色块
    face_col = {}
    for faces, c in cols:
        for f in faces:
            face_col[f] = c
    allc = np.array([face_col.get(f, np.array([0.85, 0.82, 0.9])) for f in bm.faces])
    K = max(1, min(int(colors), len(allc)))
    cent = allc[np.linspace(0, len(allc) - 1, K).astype(int)].copy()
    for _ in range(10):
        lab = np.argmin(((allc[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)
        for j in range(K):
            if (lab == j).any():
                cent[j] = allc[lab == j].mean(0)
    for f, j in zip(bm.faces, lab):
        f.material_index = int(j)
        f.smooth = False
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    lin = _srgb_to_linear(np.clip(cent, 0, 1))
    for j in range(K):
        mt = bpy.data.materials.new(f"{name}_c{j}")
        mt.use_nodes = True
        b = mt.node_tree.nodes.get("Principled BSDF")
        if b:
            b.inputs["Base Color"].default_value = (*lin[j], 1.0)
            b.inputs["Roughness"].default_value = 1.0
        me.materials.append(mt)
    ob["ignore_bounds"] = True
    H.ground_to_origin()
    info = scene_info()
    return {"puffs": n, "size": info["size"], "triangles": info["triangles"],
            "colors": ["#%02x%02x%02x" % tuple(int(max(0, min(1, x)) * 255) for x in cc) for cc in cent]}


# ───────────────────────── 配色闭环：按材质测渲染色、按比例改基色 ─────────────────────────
def _s2l(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _l2s(c):
    c = max(0.0, c)
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _inst_root(o):
    while o.parent is not None:
        o = o.parent
    return o


def _mat_base(name):
    """导入多份 GLB 时材质会被改名为 xxx.001：去掉后缀，同一资产的同一材质才能对上。"""
    head, dot, tail = name.rpartition(".")
    return head if dot and tail.isdigit() else name


def _base_socket(m):
    if not (m and m.use_nodes and m.node_tree):
        return None
    bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    return bsdf.inputs.get("Base Color") if bsdf else None


def _upstream(sock):
    """基色的上游（跳过配色闭环自己插的 _cfix 乘法节点）：返回 (节点输出 or None, 常量色)。"""
    if sock is None:
        return None, (0.8, 0.8, 0.8)
    if not sock.is_linked:
        return None, tuple(sock.default_value[:3])
    out = sock.links[0].from_socket
    node = out.node
    if node.name == "_cfix":
        a = node.inputs[6] if len(node.inputs) > 6 else node.inputs[1]   # Mix(颜色) A 输入
        return _upstream(a)
    return out, (1.0, 1.0, 1.0)


def _vcol_median(layer, m, objs):
    """用材质 m 的那些面上的顶点色中位数（线性）；找不到颜色属性时返回 None。"""
    import numpy as np
    vals = []
    for o in objs:
        me = o.data
        idx = [i for i, sl in enumerate(o.material_slots) if sl.material == m]
        if not idx:
            continue
        ca = (me.color_attributes.get(layer) if layer else None) or me.color_attributes.active_color
        if ca is None and len(me.color_attributes):
            ca = me.color_attributes[0]
        if ca is None:
            continue
        n = len(ca.data)
        arr = np.empty(n * 4, dtype=np.float32)
        ca.data.foreach_get("color", arr)
        arr = arr.reshape(n, 4)[:, :3]
        corner = ca.domain == "CORNER"
        for p in me.polygons:
            if p.material_index in idx:
                vals.append(arr[p.loop_start] if corner else arr[p.vertices[0]])
    if not vals:
        return None
    return [float(v) for v in np.median(np.array(vals), 0)]


def _eval_color(sock, m, objs, depth=0):
    """沿着基色的节点链求设定色（线性）：常量、顶点色 / 颜色属性、乘法混合（glTF 导入常把 顶点色 × 基色系数 接成这样）；
    配色闭环自己的 _cfix 节点跳过；认不出的节点按白色处理。"""
    if sock is None or depth > 8:
        return [1.0, 1.0, 1.0]
    if not sock.is_linked:
        v = sock.default_value
        try:
            return [float(v[0]), float(v[1]), float(v[2])]
        except TypeError:
            return [float(v)] * 3
    node = sock.links[0].from_socket.node
    if node.name == "_cfix":
        return _eval_color(node.inputs[6], m, objs, depth + 1)
    if node.type in ("VERTEX_COLOR", "ATTRIBUTE"):
        c = _vcol_median(getattr(node, "layer_name", "") or getattr(node, "attribute_name", ""), m, objs)
        return c or [1.0, 1.0, 1.0]
    if node.type in ("MIX", "MIX_RGB") and getattr(node, "blend_type", "") == "MULTIPLY":
        if node.type == "MIX":
            a, b = node.inputs[6], node.inputs[7]
        else:
            a, b = node.inputs[1], node.inputs[2]
        ca, cb = _eval_color(a, m, objs, depth + 1), _eval_color(b, m, objs, depth + 1)
        return [x * y for x, y in zip(ca, cb)]
    if node.type == "SEPARATE_COLOR" or node.type == "SEPRGB":
        return [1.0, 1.0, 1.0]
    return [1.0, 1.0, 1.0]


def _authored_color(m, objs):
    """材质的设定色（线性）。"""
    return _eval_color(_base_socket(m), m, objs)


def _dominant_median(px):
    """一组像素（N×3，0–1）里占多数的那一簇颜色的中位数：两簇 k-means（按亮度初始化），取像素多的一簇。
    木板之间的暗缝、物件边上的描边不会把中位色拉偏。"""
    import numpy as np
    px = np.asarray(px, dtype=np.float64).reshape(-1, 3)
    if len(px) < 20:
        return np.median(px, 0)
    lum = px @ np.array([0.2126, 0.7152, 0.0722])
    c = np.array([px[lum <= np.percentile(lum, 25)].mean(0), px[lum >= np.percentile(lum, 75)].mean(0)])
    for _ in range(8):
        lab = np.argmin(((px[:, None] - c[None]) ** 2).sum(-1), 1)
        for j in range(2):
            if (lab == j).any():
                c[j] = px[lab == j].mean(0)
    big = int(np.bincount(lab, minlength=2).argmax())
    return np.median(px[lab == big], 0)


def _target_med(tpx, m, tmask):
    import numpy as np
    mm = m & tmask if tmask is not None else m
    if int(mm.sum()) < 30:
        return {}
    return {"target": [round(float(x), 4) for x in np.median(tpx[mm], 0)], "target_px": int(mm.sum())}


def material_stats(image, camera, min_px=120, target=None, target_mask=None):
    """配色闭环的测量：与 image（同机位 ref 渲染图）同机位，渲一张「每个实例 × 材质一种平涂色」的 ID 图，
    统计每种材质在渲染图上的像素中位色（sRGB），和它的设定色（sRGB）对照。返回 [{root, material, px, rendered, authored}]。"""
    import numpy as np
    scene = bpy.context.scene
    mats = {}   # 材质名 → (材质, 实例根名, [物体])
    for o in scene.objects:
        if o.type != "MESH" or o.hide_render:
            continue
        root = _inst_root(o).name
        for sl in o.material_slots:
            m = sl.material
            if m is None:
                continue
            ent = mats.setdefault(m.name, [m, root, []])
            if o not in ent[2]:
                ent[2].append(o)
    names = sorted(mats)
    # ≥16³ IDs so large composed scenes (700+ unique materials) do not collide.
    # Spacing must stay > 2× match tolerance (0.03) or adjacent IDs bleed.
    nlev = 16
    lv = [0.04 + i * (0.92 / (nlev - 1)) for i in range(nlev)]
    ids = [(lv[i % nlev], lv[(i // nlev) % nlev], lv[(i // (nlev * nlev)) % nlev])
           for i in range(1, len(names) + 1)]
    if len(names) > nlev ** 3:
        raise RuntimeError(f"material_stats: {len(names)} materials exceed ID space {nlev**3}")
    beauty = bpy.data.images.load(image, check_existing=False)
    w, h = beauty.size
    bpx = np.array(beauty.pixels[:], dtype=np.float32).reshape(h, w, 4)[..., :3]
    bpy.data.images.remove(beauty)
    tpx = None
    if target:   # 目标图缩到同尺寸：每种材质在目标图同位置的颜色（远景海面等大面积、位置可靠的材质用它当目标色）
        ti = bpy.data.images.load(target, check_existing=False)
        ti.scale(w, h)
        tpx = np.array(ti.pixels[:], dtype=np.float32).reshape(h, w, 4)[..., :3]
        bpy.data.images.remove(ti)
    tmask = None
    if target_mask:   # 只取目标图里这块区域（如水面）的像素：海面板在岛后面的部分对应目标图上的岛，不能拿来当海色
        tm = bpy.data.images.load(target_mask, check_existing=False)
        tm.scale(w, h)
        tmask = np.array(tm.pixels[:], dtype=np.float32).reshape(h, w, 4)[..., 0] > 0.5
        bpy.data.images.remove(tm)
    r = scene.render
    ev = scene.eevee
    keep = (r.engine, r.film_transparent, r.resolution_x, r.resolution_y, r.filepath, scene.view_settings.exposure,
            scene.view_settings.view_transform, scene.view_settings.look, scene.camera, r.filter_size,
            getattr(ev, "taa_render_samples", None))
    path = os.path.join(os.path.dirname(image), "_matid.png")
    idmats, swapped = {}, []
    try:
        # ID 图用 EEVEE + 每种材质一个纯自发光材质：自发光不受光照影响，颜色精确；
        # 背面剔除沿用原材质（描边外壳在正常渲染里被剔掉，ID 图里也必须剔掉，否则会盖住岛顶）
        for n, c in zip(names, ids):
            src = mats[n][0]
            im = bpy.data.materials.new("_matid_" + str(len(idmats)))
            im.use_nodes = True
            nt = im.node_tree
            for nd in list(nt.nodes):
                nt.nodes.remove(nd)
            em = nt.nodes.new("ShaderNodeEmission")
            em.inputs[0].default_value = (*[_s2l(x) for x in c], 1.0)
            em.inputs[1].default_value = 1.0
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            nt.links.new(em.outputs[0], out.inputs[0])
            im.use_backface_culling = bool(getattr(src, "use_backface_culling", False))
            idmats[n] = im
        for o in scene.objects:
            if o.type != "MESH":
                continue
            for k, sl in enumerate(o.material_slots):
                if sl.material is not None and sl.material.name in idmats:
                    swapped.append((o, k, sl.material))
                    sl.material = idmats[sl.material.name]
        r.engine = _engine("BLENDER_EEVEE")
        r.film_transparent = True
        r.filter_size = 0.0
        if keep[-1] is not None:
            ev.taa_render_samples = 1
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        r.resolution_x, r.resolution_y = w, h
        cam_data = bpy.data.cameras.get("_pipe_cam") or bpy.data.cameras.new("_pipe_cam")
        cam = bpy.data.objects.get("_pipe_cam") or bpy.data.objects.new("_pipe_cam", cam_data)
        if cam.name not in scene.collection.objects:
            scene.collection.objects.link(cam)
        fov = math.radians(max(10.0, min(100.0, float(camera.get("fov_deg", 40)))))
        cam_data.sensor_fit = "HORIZONTAL"
        lens0 = cam_data.lens
        cam_data.lens = 18 / math.tan(fov / 2)
        cam.location = Vector([float(x) for x in camera["eye"]])
        cam.rotation_euler = (Vector([float(x) for x in camera["target"]]) - cam.location).to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam
        r.filepath = path
        bpy.ops.render.render(write_still=True)
        cam_data.lens = lens0
        idimg = bpy.data.images.load(path, check_existing=False)
        ipx = np.array(idimg.pixels[:], dtype=np.float32).reshape(h, w, 4)
        bpy.data.images.remove(idimg)
    finally:
        for o, k, mat in swapped:
            o.material_slots[k].material = mat
        for im in idmats.values():
            bpy.data.materials.remove(im)
        (r.engine, r.film_transparent, r.resolution_x, r.resolution_y, r.filepath, scene.view_settings.exposure,
         scene.view_settings.view_transform, scene.view_settings.look, scene.camera, r.filter_size, taa) = keep
        if taa is not None:
            ev.taa_render_samples = taa
    solid = ipx[..., 3] > 0.99
    rgb = ipx[..., :3]
    res = []
    root_px = {}
    root_px_flower = {}  # petal/center only — flowers must not mix stem/grass ID collisions
    flower_roots = set()
    _keep = ("petal", "center")
    _skip = ("stem", "leaf", "outline", "trunk", "grass")
    # 1px binary erosion reduces beauty-AA fringe into thin stems before median
    try:
        from numpy.lib.stride_tricks import sliding_window_view
        def _erode1(mask):
            if mask.shape[0] < 3 or mask.shape[1] < 3:
                return mask
            # pad then require 3x3 all-true
            p = np.pad(mask, 1, constant_values=False)
            win = sliding_window_view(p, (3, 3))
            return win.all(axis=(-1, -2))
    except Exception:
        def _erode1(mask):
            return mask
    for n, c in zip(names, ids):
        m = solid & (np.abs(rgb - np.array(c, dtype=np.float32)).max(-1) < 0.03)
        k = int(m.sum())
        base = _mat_base(n).lower()
        root = mats[n][1]
        if any(s in base for s in _keep):
            flower_roots.add(root)
        if k:
            rp = root_px.get(root)
            root_px[root] = m if rp is None else (rp | m)
            if any(s in base for s in _keep):
                rf = root_px_flower.get(root)
                root_px_flower[root] = m if rf is None else (rf | m)
        # Flower petal/center: keep low-px rows (true petals are tiny once ID collisions stop)
        is_flower_part = any(s in base for s in _keep)
        if k < (15 if is_flower_part else min_px):
            continue
        # erode before beauty sample when mask is large enough (skip erode for tiny flower parts)
        m_s = _erode1(m) if (k >= 80 and not is_flower_part) else m
        if int(m_s.sum()) < max(8, (10 if is_flower_part else min_px // 4)):
            m_s = m
        mat, root, objs = mats[n]
        med = np.median(bpx[m_s], 0)
        auth = _authored_color(mat, objs)
        res.append({"root": root, "material": _mat_base(n), "px": int(m_s.sum()),
                    "rendered": [round(float(x), 4) for x in med],
                    "authored": [round(_l2s(x), 4) for x in auth],
                    **(_target_med(tpx, m_s, tmask) if tpx is not None else {})})
    # 每个实例自己的像素：花类实例优先 petal/center；阈值降到 10（穗花真实可见像素很少）
    roots_out = {}
    for rt, m in root_px.items():
        flower_m = root_px_flower.get(rt)
        if rt in flower_roots and flower_m is not None and flower_m.sum() >= 10:
            use = flower_m
        else:
            use = m
        if use.sum() < 10:
            continue
        # don't erode tiny flower masks
        use_s = _erode1(use) if (use.sum() >= 80 and rt not in flower_roots) else use
        if use_s.sum() < 10:
            use_s = use
        roots_out[rt] = {"px": int(use_s.sum()),
                         "rendered": [round(float(x), 4) for x in _dominant_median(bpx[use_s])]}
    return {"materials": res, "roots": roots_out, "size": [w, h],
            "id_space": nlev ** 3, "n_materials": len(names)}


def recolor(fixes, roots):
    """按配色闭环给出的系数改材质基色：fixes = {资产 id: {材质名: [r,g,b] 线性乘数}}，roots = {实例名: 资产 id}。
    在基色前插一个乘法节点（_cfix）：顶点色、贴图、常量色都一样乘上去；系数再变只改这个节点。"""
    scene = bpy.context.scene
    done = 0
    seen = set()
    for o in scene.objects:
        if o.type != "MESH":
            continue
        asset = roots.get(_inst_root(o).name)
        fx = (fixes.get(asset) if asset else None) or {}
        for sl in o.material_slots:
            m = sl.material
            if m is None or m.name in seen:
                continue
            f = fx.get(_mat_base(m.name))
            sock = _base_socket(m)
            if sock is None:
                continue
            if not f:
                if m.node_tree.nodes.get("_cfix") is None:
                    continue
                f = [1.0, 1.0, 1.0]   # 退回：之前改过、这次不在系数表里的材质恢复原色
            seen.add(m.name)
            nt = m.node_tree
            node = nt.nodes.get("_cfix")
            if node is None:
                up, const = _upstream(sock)
                node = nt.nodes.new("ShaderNodeMix")
                node.name = "_cfix"
                node.data_type = "RGBA"
                node.blend_type = "MULTIPLY"
                node.inputs[0].default_value = 1.0
                if up is not None:
                    nt.links.new(up, node.inputs[6])
                else:
                    node.inputs[6].default_value = (*const, 1.0)
                nt.links.new(node.outputs[2], sock)
            node.inputs[7].default_value = (*[max(0.0, float(x)) for x in f[:3]], 1.0)
            m.diffuse_color = (*[min(1.0, a * b) for a, b in zip(_upstream(sock)[1], f[:3])], 1.0)
            done += 1
    return {"recolored": done}


def normal_pass(camera, size, out):
    """法线通道：同机位渲一张「相机坐标法线」图（OpenCV：x 右、y 下、z 向前；存成 n·0.5+0.5，透明处无几何）。
    布光闭环用它在渲染图上按法线·光向拟合亮暗比，和目标图（深度模型法线）比的是同一个量。"""
    scene = bpy.context.scene
    r = scene.render
    ev = scene.eevee
    w, h = int(size[0]), int(size[1])
    keep = (r.engine, r.film_transparent, r.resolution_x, r.resolution_y, r.filepath, scene.view_settings.exposure,
            scene.view_settings.view_transform, scene.view_settings.look, scene.camera, r.filter_size,
            getattr(ev, "taa_render_samples", None))
    mats, swapped = {}, []

    def nmat(cull):
        if cull in mats:
            return mats[cull]
        m = bpy.data.materials.new(f"_normal_pass_{int(cull)}")
        m.use_nodes = True
        nt = m.node_tree
        for nd in list(nt.nodes):
            nt.nodes.remove(nd)
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        vt = nt.nodes.new("ShaderNodeVectorTransform")
        vt.vector_type, vt.convert_from, vt.convert_to = "NORMAL", "WORLD", "CAMERA"
        mul = nt.nodes.new("ShaderNodeVectorMath")
        mul.operation = "MULTIPLY_ADD"
        mul.inputs[1].default_value = (0.5, -0.5, 0.5)     # 着色节点的相机系：y 上、z 朝画面里 → OpenCV（y 下、z 向前），再映射到 0–1
        mul.inputs[2].default_value = (0.5, 0.5, 0.5)
        em = nt.nodes.new("ShaderNodeEmission")
        outn = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(geo.outputs["Normal"], vt.inputs[0])
        nt.links.new(vt.outputs[0], mul.inputs[0])
        nt.links.new(mul.outputs[0], em.inputs[0])
        nt.links.new(em.outputs[0], outn.inputs[0])
        m.use_backface_culling = cull
        mats[cull] = m
        return m

    try:
        for o in scene.objects:
            if o.type != "MESH" or o.hide_render:
                continue
            for k, sl in enumerate(o.material_slots):
                if sl.material is not None:
                    swapped.append((o, k, sl.material))
                    sl.material = nmat(bool(getattr(sl.material, "use_backface_culling", False)))
        r.engine = _engine("BLENDER_EEVEE")
        r.film_transparent = True
        r.filter_size = 0.0
        if keep[-1] is not None:
            ev.taa_render_samples = 1
        scene.view_settings.view_transform = "Raw"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        r.resolution_x, r.resolution_y = w, h
        cam_data = bpy.data.cameras.get("_pipe_cam") or bpy.data.cameras.new("_pipe_cam")
        cam = bpy.data.objects.get("_pipe_cam") or bpy.data.objects.new("_pipe_cam", cam_data)
        if cam.name not in scene.collection.objects:
            scene.collection.objects.link(cam)
        fov = math.radians(max(10.0, min(100.0, float(camera.get("fov_deg", 40)))))
        cam_data.sensor_fit = "HORIZONTAL"
        lens0 = cam_data.lens
        cam_data.lens = 18 / math.tan(fov / 2)
        cam.location = Vector([float(x) for x in camera["eye"]])
        cam.rotation_euler = (Vector([float(x) for x in camera["target"]]) - cam.location).to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam
        r.filepath = out
        bpy.ops.render.render(write_still=True)
        cam_data.lens = lens0
    finally:
        for o, k, m in swapped:
            o.material_slots[k].material = m
        for m in mats.values():
            bpy.data.materials.remove(m)
        (r.engine, r.film_transparent, r.resolution_x, r.resolution_y, r.filepath, scene.view_settings.exposure,
         scene.view_settings.view_transform, scene.view_settings.look, scene.camera, r.filter_size, taa) = keep
        if taa is not None:
            ev.taa_render_samples = taa
    return {"path": out}


OPS = {
    "ping": lambda: {"pong": True, "version": bpy.app.version_string, "background": bpy.app.background,
                     "code": CODE_HASH},
    "exec": op_exec,
    "scene_info": scene_info,
    "reset": lambda: (H.clear(), scene_info())[1],
    "render": render_views,
    "build_blueprint": build_blueprint,
    "build_oskar": build_oskar,
    "compose_scene": compose_scene,
    "compose_planet": compose_planet,
    "export_glb": export_glb,
    "oskar_look": oskar_look,
    "save_blend": save_blend,
    "open_blend": open_blend,
    "import_model": import_model,
    "refine_generated": refine_generated,
    "fit_to_size": fit_to_size,
    "build_backdrop": build_backdrop,
    "material_stats": material_stats,
    "recolor": recolor,
    "normal_pass": normal_pass,
    "quit": lambda: {"bye": True},
}


def handle(line):
    req = json.loads(line)
    rid = req.get("id")
    try:
        fn = OPS[req["op"]]
        res = fn(**(req.get("args") or {}))
        return {"id": rid, "ok": True, "result": res}
    except Exception as e:
        tb = traceback.format_exc(limit=6)
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb[-4000:]}


def serve_blocking(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"[worker] listening on {port}", flush=True)
    while True:
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        try:
            for raw in f:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                resp = handle(line)
                f.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                f.flush()
                if json.loads(line).get("op") == "quit":
                    conn.close()
                    srv.close()
                    return
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class _TimerServer:
    """有界面模式：非阻塞 socket + bpy.app.timers，在主线程执行请求。"""

    def __init__(self, port):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(4)
        self.srv.setblocking(False)
        self.clients = []
        print(f"[worker] (UI) listening on {port}", flush=True)

    def tick(self):
        try:
            c, _ = self.srv.accept()
            c.setblocking(False)
            self.clients.append([c, b""])
        except BlockingIOError:
            pass
        for entry in list(self.clients):
            c = entry[0]
            try:
                data = c.recv(1 << 20)
                if not data:
                    self.clients.remove(entry)
                    c.close()
                    continue
                entry[1] += data
            except BlockingIOError:
                pass
            except OSError:
                self.clients.remove(entry)
                continue
            while b"\n" in entry[1]:
                line, entry[1] = entry[1].split(b"\n", 1)
                if line.strip():
                    resp = handle(line.decode("utf-8"))
                    c.setblocking(True)
                    c.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    c.setblocking(False)
        return 0.05


if bpy.app.background:
    serve_blocking(ARGS["port"])
else:
    _srv = _TimerServer(ARGS["port"])
    bpy.app.timers.register(_srv.tick, persistent=True)
