"""Phase 15 —— 程序化材质 + 金色田园布光（Blender 内运行，纯 bpy，无外部贴图）。

背景：11–14 阶段一直在加道具、追指标，但渲染始终是「平的、灰的」。根因是组装场景里
733 个材质全是 GLB 默认灰 (0.8,0.8,0.8) 的单色 Principled，没有任何程序化纹理节点，
EEVEE 拿不到表面细节；而岛体网格自带的顶点色 layer「Color」也没被任何材质使用。

本模块做三件事：
1. `build_materials()` —— 按材质名前缀把整个场景升级成程序化着色（草地/岩石/毛石/
   灰泥/陶瓦/木/叶/花瓣/水），用 Noise/Voronoi/Wave/Brick + ColorRamp 驱动 Principled。
   草地同时采样网格自带的顶点色，把原来被丢弃的颜色变化接回来。
2. `golden_pastoral()` —— 「金色田园」灯光：低角度暖主光 + 冷天光补光 + 暖轮廓光，
   开启 EEVEE 光线追踪 / 屏幕空间 AO / 体积雾，相机看到深蓝天色而场景吃亮天光
   （两路 Background 用 Light Path 分开，避免天色把整个场景压蓝）。
3. `apply_look()` —— 上面两件事的入口，供 worker.render_views 调用。
"""
import math
import random

import bpy
import numpy as np
from mathutils import Matrix, Vector

# ---------------------------------------------------------------- utilities
def srgb2lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def L(rgb):
    return tuple(srgb2lin(v) for v in rgb)


def srgb2lin_c(rgb):
    return tuple(srgb2lin(v) for v in rgb)


def rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def tilt(ax, ay):
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    return np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], float) @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], float)


def merge(pv, pf, tf, name, material, smooth=False):
    """instance a prototype (verts pv, faces pf) at transforms tf = [(P, R, S)]"""
    pv = np.asarray(pv, dtype=float)
    if pv.shape[1] > 3:
        pv = pv[:, :3]
    nv, nf, N = len(pv), len(pf), len(tf)
    V = np.zeros((N * nv, 3), np.float32)
    F = np.zeros((N * nf, 3), np.int32)
    for i, (Pp, R, S) in enumerate(tf):
        V[i * nv:(i + 1) * nv] = (pv * S) @ R.T + Pp
        F[i * nf:(i + 1) * nf] = np.array(pf, np.int32) + i * nv
    me = bpy.data.meshes.new(name)
    me.from_pydata(V.tolist(), [], F.tolist())
    me.validate()
    me.materials.append(material)
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    for p in me.polygons:
        p.use_smooth = smooth
    return ob


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)


# ------------------------------------------------------- procedural shaders
class Shader:
    """small helper to build a Principled node graph"""

    def __init__(self, mat):
        mat.use_nodes = True
        self.mat = mat
        self.nt = mat.node_tree
        self.nt.nodes.clear()
        self.out = self.nt.nodes.new("ShaderNodeOutputMaterial")
        self.bsdf = self.nt.nodes.new("ShaderNodeBsdfPrincipled")
        self.nt.links.new(self.bsdf.outputs[0], self.out.inputs["Surface"])
        self.geo = self.nt.nodes.new("ShaderNodeNewGeometry")
        self.pos = self.geo.outputs["Position"]      # world-space position

    # ---- primitives
    def noise(self, scale=6.0, detail=6.0, rough=0.55, dim=3.0, wobble=0.0, y=None):
        n = self.nt.nodes.new("ShaderNodeTexNoise")
        n.inputs["Scale"].default_value = scale
        n.inputs["Detail"].default_value = detail
        n.inputs["Roughness"].default_value = rough
        try:
            n.inputs["Distortion"].default_value = wobble
        except Exception:
            pass
        self.nt.links.new(self.pos, n.inputs["Vector"])
        return n.outputs["Fac"]

    def voronoi(self, scale=25.0, feature="F1", rand=1.0):
        v = self.nt.nodes.new("ShaderNodeTexVoronoi")
        v.feature = feature
        v.inputs["Scale"].default_value = scale
        try:
            v.inputs["Randomness"].default_value = rand
        except Exception:
            pass
        self.nt.links.new(self.pos, v.inputs["Vector"])
        return v.outputs["Distance"] if feature != "DISTANCE_TO_EDGE" else v.outputs["Distance"]

    def voro_color(self, scale=4.0, rand=1.0):
        v = self.nt.nodes.new("ShaderNodeTexVoronoi")
        v.feature = "F1"
        v.inputs["Scale"].default_value = scale
        try:
            v.inputs["Randomness"].default_value = rand
        except Exception:
            pass
        self.nt.links.new(self.pos, v.inputs["Vector"])
        return v.outputs["Color"]

    def wave(self, scale=8.0, distortion=2.0, detail=2.0, direction="X", kind="BANDS"):
        w = self.nt.nodes.new("ShaderNodeTexWave")
        w.wave_type = kind
        try:
            w.bands_direction = direction
        except Exception:
            pass
        w.inputs["Scale"].default_value = scale
        w.inputs["Distortion"].default_value = distortion
        w.inputs["Detail"].default_value = detail
        self.nt.links.new(self.pos, w.inputs["Vector"])
        return w.outputs["Fac"]

    def brick(self, scale=4.0, mortar=0.03, bw=0.5, rh=0.25, c1=(0.6, 0.5, 0.4), c2=(0.5, 0.42, 0.33), mc=(0.3, 0.27, 0.23)):
        b = self.nt.nodes.new("ShaderNodeTexBrick")
        b.inputs["Scale"].default_value = scale
        b.inputs["Mortar Size"].default_value = mortar
        b.inputs["Brick Width"].default_value = bw
        b.inputs["Row Height"].default_value = rh
        b.inputs["Color1"].default_value = (*L(c1), 1)
        b.inputs["Color2"].default_value = (*L(c2), 1)
        b.inputs["Mortar"].default_value = (*L(mc), 1)
        self.nt.links.new(self.pos, b.inputs["Vector"])
        return b.outputs["Color"]

    def ramp(self, fac, stops):
        r = self.nt.nodes.new("ShaderNodeValToRGB")
        cr = r.color_ramp
        while len(cr.elements) > 1:
            cr.elements.remove(cr.elements[-1])
        cr.elements[0].position = stops[0][0]
        cr.elements[0].color = (*L(stops[0][1]), 1)
        for pos, col in stops[1:]:
            e = cr.elements.new(pos)
            e.color = (*L(col), 1)
        self.nt.links.new(fac, r.inputs["Fac"])
        return r.outputs["Color"]

    def mix(self, fac, a, b, blend="MIX"):
        mx = self.nt.nodes.new("ShaderNodeMix")
        mx.data_type = "RGBA"
        mx.blend_type = blend
        if hasattr(fac, "is_output"):
            self.nt.links.new(fac, mx.inputs[0])
        else:
            mx.inputs[0].default_value = fac
        ai = [i for i in mx.inputs if i.type == "RGBA"]
        for sock, val in zip(ai, (a, b)):
            if hasattr(val, "is_output"):
                self.nt.links.new(val, sock)
            else:
                sock.default_value = (*(val if len(val) == 3 else val[:3]), 1)
        return [o for o in mx.outputs if o.type == "RGBA"][0]

    def bump(self, height, strength=0.25, dist=0.06):
        b = self.nt.nodes.new("ShaderNodeBump")
        b.inputs["Strength"].default_value = strength
        b.inputs["Distance"].default_value = dist
        self.nt.links.new(height, b.inputs["Height"])
        self.nt.links.new(b.outputs["Normal"], self.bsdf.inputs["Normal"])
        return b

    def set(self, color=None, rough=0.85, spec=0.15):
        if color is not None:
            if hasattr(color, "is_output"):
                self.nt.links.new(color, self.bsdf.inputs["Base Color"])
            else:
                self.bsdf.inputs["Base Color"].default_value = (*(color if len(color) == 3 else color[:3]), 1)
        self.bsdf.inputs["Roughness"].default_value = rough
        try:
            self.bsdf.inputs["Specular IOR Level"].default_value = spec
        except Exception:
            pass


# ---- family builders -----------------------------------------------------
def mat_meadow(mat, vcol=False):
    s = Shader(mat)
    n = s.noise(scale=2.2, detail=8.0, rough=0.62, wobble=0.25)
    base = s.ramp(n, [(0.30, (0.13, 0.22, 0.075)), (0.48, (0.24, 0.36, 0.11)),
                      (0.66, (0.38, 0.45, 0.155)), (0.84, (0.52, 0.50, 0.20))])
    # small bright speckles -> reads as a distant flower field
    sp = s.voronoi(scale=45.0, feature="F1")
    dots = s.ramp(sp, [(0.10, (1.0, 0.55, 0.30)), (0.18, (0.0, 0.0, 0.0))])
    mask = s.ramp(sp, [(0.08, (0.85, 0.85, 0.85)), (0.16, (0.0, 0.0, 0.0))])
    speck = s.mix(mask, base, dots, "MULTIPLY")
    col = s.mix(0.55, base, speck, "MULTIPLY")
    if vcol:
        vc = s.nt.nodes.new("ShaderNodeVertexColor")
        vc.layer_name = "Color"
        # keep the painted variation but pull it toward the meadow palette
        tint = s.mix(0.42, (1, 1, 1), vc.outputs["Color"], "MULTIPLY")
        col = s.mix(0.62, col, tint, "MULTIPLY")
    s.bump(s.noise(scale=22.0, detail=6.0), 0.35, 0.05)
    s.set(col, 0.92, 0.10)


def mat_rock(mat):
    s = Shader(mat)
    n = s.noise(scale=3.4, detail=8.0, rough=0.65, wobble=0.35)
    v = s.voronoi(scale=9.0, feature="DISTANCE_TO_EDGE")
    base = s.ramp(n, [(0.26, (0.30, 0.235, 0.165)), (0.48, (0.45, 0.36, 0.245)),
                      (0.72, (0.58, 0.47, 0.325)), (0.90, (0.70, 0.60, 0.44))])
    cracks = s.ramp(v, [(0.0, (0.42, 0.42, 0.42)), (0.045, (1, 1, 1))])
    col = s.mix(cracks, base, (0.16, 0.13, 0.10), "MULTIPLY")
    col = s.mix(0.45, col, (0.58, 0.44, 0.26), "OVERLAY")
    s.bump(s.noise(scale=16.0, detail=7.0), 0.45, 0.09)
    s.set(col, 0.88, 0.14)


def mat_stone(mat):
    s = Shader(mat)
    b = s.brick(scale=3.2, mortar=0.018, bw=0.62, rh=0.30,
                c1=(0.70, 0.60, 0.45), c2=(0.58, 0.49, 0.36), mc=(0.40, 0.34, 0.26))
    n = s.noise(scale=7.0, detail=8.0, rough=0.6, wobble=0.2)
    grime = s.ramp(n, [(0.35, (0.72, 0.70, 0.66)), (0.75, (1.0, 1.0, 1.0))])
    col = s.mix(0.70, b, grime, "MULTIPLY")
    s.bump(s.noise(scale=30.0, detail=7.0), 0.30, 0.03)
    s.set(col, 0.80, 0.20)


def mat_plaster(mat):
    s = Shader(mat)
    n = s.noise(scale=5.0, detail=8.0, rough=0.55, wobble=0.15)
    col = s.ramp(n, [(0.30, (0.62, 0.51, 0.36)), (0.62, (0.78, 0.67, 0.49)), (0.88, (0.88, 0.79, 0.61))])
    s.bump(s.noise(scale=42.0, detail=6.0), 0.18, 0.02)
    s.set(col, 0.86, 0.12)


def mat_roof(mat):
    """terracotta ridged tiles"""
    s = Shader(mat)
    ridge = s.wave(scale=11.0, distortion=0.35, detail=1.0, direction="X", kind="BANDS")
    # ridges modulate between two warm terracotta tones (never toward grey/white)
    rmask = s.ramp(ridge, [(0.20, (0.86, 0.45, 0.24)), (0.50, (1.0, 0.72, 0.46)), (0.82, (0.78, 0.36, 0.17))])
    tiles = s.brick(scale=7.0, mortar=0.010, bw=0.44, rh=0.60,
                    c1=(0.80, 0.40, 0.18), c2=(0.69, 0.31, 0.13), mc=(0.48, 0.20, 0.09))
    n = s.noise(scale=10.0, detail=6.0, rough=0.5, wobble=0.3)
    var = s.ramp(n, [(0.30, (0.88, 0.80, 0.72)), (0.72, (1.0, 0.96, 0.90))])
    col = s.mix(0.60, tiles, var, "MULTIPLY")
    col = s.mix(0.60, col, rmask, "MULTIPLY")
    s.bump(ridge, 0.50, 0.05)
    s.set(col, 0.72, 0.30)


def mat_wood(mat):
    s = Shader(mat)
    g = s.wave(scale=14.0, distortion=3.0, detail=3.0, direction="Z")
    n = s.noise(scale=4.0, detail=7.0, rough=0.6)
    col = s.ramp(g, [(0.20, (0.26, 0.14, 0.07)), (0.55, (0.42, 0.25, 0.13)), (0.85, (0.54, 0.35, 0.19))])
    col = s.mix(0.35, col, s.ramp(n, [(0.3, (0.72, 0.68, 0.62)), (0.8, (1, 1, 1))]), "MULTIPLY")
    s.bump(g, 0.30, 0.02)
    s.set(col, 0.82, 0.16)


def mat_leaf(mat, dark=False):
    s = Shader(mat)
    n = s.noise(scale=6.0, detail=8.0, rough=0.6, wobble=0.35)
    lo = (0.048, 0.092, 0.040) if dark else (0.062, 0.118, 0.052)
    mid = (0.085, 0.160, 0.062) if dark else (0.105, 0.195, 0.080)
    hi = (0.140, 0.225, 0.088) if dark else (0.170, 0.275, 0.110)
    col = s.ramp(n, [(0.28, lo), (0.55, mid), (0.85, hi)])
    # leaf-scale speckle so big canopies are not flat plates
    v = s.voronoi(scale=22.0, feature="F1")
    sp = s.ramp(v, [(0.0, (1.25, 1.20, 1.05)), (0.55, (0.80, 0.86, 0.78))])
    col = s.mix(0.70, col, sp, "MULTIPLY")
    s.bump(s.noise(scale=48.0, detail=8.0, rough=0.72), 0.55, 0.05)
    s.set(col, 0.88, 0.12)


def mat_petal(mat, keep_rgb):
    s = Shader(mat)
    n = s.noise(scale=9.0, detail=5.0, rough=0.5)
    # vary brightness around the authored colour so a mass reads as thousands
    v = s.ramp(n, [(0.25, (0.80, 0.62, 0.58)), (0.55, (1.0, 0.94, 0.90)), (0.82, (1.0, 1.0, 1.0))])
    base = tuple(c ** (1 / 2.2) if c > 0.004 else c for c in keep_rgb)   # linear->approx sRGB for ramp use
    col = s.mix(0.62, keep_rgb, v, "MULTIPLY")
    s.set(col, 0.70, 0.30)


def mat_water(mat):
    s = Shader(mat)
    n = s.noise(scale=0.55, detail=8.0, rough=0.55, wobble=0.6)
    col = s.ramp(n, [(0.30, (0.055, 0.16, 0.30)), (0.55, (0.09, 0.24, 0.40)), (0.80, (0.16, 0.36, 0.50))])
    s.bsdf.inputs["Roughness"].default_value = 0.16
    try:
        s.bsdf.inputs["Metallic"].default_value = 0.35
    except Exception:
        pass
    s.bump(s.noise(scale=1.6, detail=6.0, wobble=0.4), 0.22, 0.10)
    s.set(col, 0.16, 0.55)


def mat_outline(mat):
    s = Shader(mat)
    s.set((0.026, 0.022, 0.026), 1.0, 0.02)


# ---- classification ------------------------------------------------------
def family(name):
    n = name.split(".")[0]
    if n.startswith("osk_outline") or n.startswith("Material") or n == "Dots Stroke":
        return "outline"
    if n.startswith("osk_grass"):
        return "meadow"
    if n.startswith("osk_rock"):
        return "rock"
    if n.startswith("osk_roof"):
        return "roof"
    if n.startswith("osk_plaster"):
        return "plaster"
    if n.startswith("osk_door") or n.startswith("osk_wood"):
        return "wood"
    if n.startswith("osk_window"):
        return "window"
    if n.startswith("osk_stone") or n.startswith("osk_trim") or n.startswith("osk_foam"):
        return "stone"
    if n.startswith("stone_arch_bridge") or n.startswith("stone_path_gate") or n.startswith("stone_bridge"):
        return "stone"
    if n.startswith("osk_leaf") or n.startswith("osk_canopy") or n.startswith("green_foliage"):
        return "leaf"
    if n.startswith("osk_petal") or n.startswith("osk_center") or n.startswith("osk_stem"):
        return "petal"
    if n.startswith("sea_sky"):
        return "water"
    if n.startswith("broadleaf_tree"):
        return "leaf"
    return None


def kill_tiered_props():
    """the six oversized flower props floating at roof height read as red tiered cylinders"""
    gone = 0
    for i in (67, 68, 69, 70, 91, 92):
        o = bpy.data.objects.get("oskar_props.%03d" % i)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
            gone += 1
    print("P15_KILL_TIERED", gone)


def break_up_foliage(cfg):
    """The canopy trees are smooth low-poly spheres; displacement is the only thing that
    makes their silhouette read as foliage rather than green blobs."""
    strength = cfg.get("foliage_disp", 0.16)
    levels = int(cfg.get("foliage_subdiv", 1))
    if strength <= 0:
        return
    tex = bpy.data.textures.get("p15_clouds") or bpy.data.textures.new("p15_clouds", type="CLOUDS")
    tex.noise_scale = 0.085
    tex.noise_depth = 3
    n = 0
    CYPRESS = {"oskar_scene.%03d" % i for i in range(12, 22)}
    targets = [o for o in bpy.data.objects
               if o.type == "MESH" and (o.name.startswith("oskar_scene.")
                                        or o.name.startswith("broadleaf_tree"))
               and o.name not in CYPRESS
               and any((m and "kanopy" in m.name or (m and m.name.startswith("osk_leaf")))
                       for m in o.data.materials)]
    for o in targets:
        try:
            bpy.context.view_layer.objects.active = o
            sub = o.modifiers.new("p15sub", "SUBSURF")
            sub.subdivision_type = "SIMPLE"
            sub.levels = levels
            sub.render_levels = levels
            bpy.ops.object.modifier_apply(modifier=sub.name)
            dsp = o.modifiers.new("p15disp", "DISPLACE")
            dsp.texture = tex
            dsp.strength = strength * max(0.4, (o.dimensions.x + o.dimensions.y) / 2.0)
            dsp.mid_level = 0.42
            dsp.texture_coords = "GLOBAL"
            bpy.ops.object.modifier_apply(modifier=dsp.name)
            for p in o.data.polygons:
                p.use_smooth = False
            n += 1
        except Exception as e:
            print("P15_FOLIAGE_SKIP", o.name, str(e)[:60])
    print("P15_FOLIAGE_DISPLACED", n)




# --------------------------------------------------------------- public API
#: 金色田园预设参数（与 scripts/p15_materials.py 的 cfg 同名字段一一对应）
GOLDEN_PASTORAL = {
    "exposure": 1.80,
    "key": 14.0, "key_rot": (62, 4, -40),
    "fill": 0.45, "rim": 1.80,
    "sky": (0.16, 0.31, 0.50), "sky_strength": 0.90,
    "sky_cam": (0.085, 0.155, 0.25), "sky_cam_strength": 1.0,
    "samples": 64, "haze": 1, "haze_density": 0.014,
    "shadow_rays": 8, "shadow_steps": 16, "gi_rays": 4, "gi_quality": 1.0, "gi_dist": 1.2,
    "raytracing": True,
}


def build_materials(verbose=True):
    """把场景里所有材质替换成程序化着色图；返回各族的替换数量。"""
    counts = {}
    for m in list(bpy.data.materials):
        if not m.users:
            continue
        fam = family(m.name)
        if fam is None:
            continue
        b = m.node_tree.nodes.get("Principled BSDF") if m.use_nodes else None
        base = tuple(b.inputs["Base Color"].default_value[:3]) if b else (0.8, 0.8, 0.8)
        if fam == "outline":
            mat_outline(m)
        elif fam == "meadow":
            mat_meadow(m, vcol=True)
        elif fam == "rock":
            mat_rock(m)
        elif fam == "stone":
            mat_stone(m)
        elif fam == "plaster":
            mat_plaster(m)
        elif fam == "roof":
            mat_roof(m)
        elif fam == "wood":
            mat_wood(m)
        elif fam == "window":
            s = Shader(m)
            s.set((0.03, 0.045, 0.06), 0.25, 0.5)
        elif fam == "leaf":
            mat_leaf(m, dark=("canopy" in m.name))
        elif fam == "petal":
            mat_petal(m, base)
        elif fam == "water":
            mat_water(m)
        counts[fam] = counts.get(fam, 0) + 1
    if verbose:
        print("P15_MATERIALS", dict(sorted(counts.items(), key=lambda x: -x[1])))
    return counts


def golden_lighting(scene, cfg=None):
    """金色田园布光：暖色主光（从相机一侧斜上方打来）+ 冷色天光补光 + 轮廓光。

    正交平视相机只能看见朝向 -Y 的面，所以主光必须从 -Y 一侧照过来，否则整个
    场景都是背光死黑——这是之前 r627 全黑的原因。光位按「相机在 -Y、朝 +Y 看」定：
        key  : 从前上方（-Y, +Z）斜打，偏左 40°
        fill : 从右前方低角度补，冷色
        rim  : 从后上方（+Y）打亮边缘
    """
    merged = dict(GOLDEN_PASTORAL)
    merged.update(cfg or {})
    c = merged

    # ---- world: deep blue sky plus a warm ground bounce
    w = scene.world or bpy.data.worlds.new("p16_world")
    scene.world = w
    w.use_nodes = True
    nt = w.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    sky = c.get("sky", (0.16, 0.31, 0.50))
    bg.inputs["Color"].default_value = (sky[0], sky[1], sky[2], 1.0)
    bg.inputs["Strength"].default_value = c.get("sky_strength", 0.75)
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    try:
        w.use_nodes = True
    except Exception:
        pass

    # ---- remove whatever lights came in with the file, then build the rig
    for o in [o for o in bpy.data.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(o, do_unlink=True)

    def add(name, kind, energy, rgb, rot, size=0.0, angle=None):
        ld = bpy.data.lights.new(name, kind)
        ld.energy = energy
        ld.color = rgb
        if kind == "SUN" and angle is not None:
            ld.angle = angle
        if kind == "AREA":
            ld.size = size
        ob = bpy.data.objects.new(name, ld)
        bpy.context.collection.objects.link(ob)
        ob.rotation_euler = tuple(math.radians(a) for a in rot)
        return ob

    # main light: from the camera side, high left — this is the "golden hour" key
    key = add("p16_key", "SUN", c.get("key", 5.0),
              tuple(c.get("key_rgb", (1.0, 0.86, 0.66))),
              c.get("key_rot_ortho", (58, 0, -38)), angle=math.radians(6.0))
    # fill: cool, low, from the other side, no shadow so it just lifts the shadow side
    fill = add("p16_fill", "SUN", c.get("fill", 1.1),
               tuple(c.get("fill_rgb", (0.72, 0.82, 1.0))),
               c.get("fill_rot_ortho", (72, 0, 62)))
    fill.data.use_shadow = False
    # rim: from behind/above to separate silhouettes from the backdrop
    rim = add("p16_rim", "SUN", c.get("rim", 2.0),
              tuple(c.get("rim_rgb", (1.0, 0.92, 0.78))),
              (118, 0, 168))
    rim.data.use_shadow = False

    # ---- render settings tuned for the ortho shot
    scene.render.engine = "BLENDER_EEVEE"
    try:
        scene.eevee.taa_render_samples = int(c.get("samples", 64))
    except Exception:
        pass
    for attr, val in (("use_gtao", True), ("gtao_distance", 0.35), ("use_shadows", True),
                      ("shadow_ray_count", 4), ("use_bloom", False),
                      ("use_raytracing", bool(c.get("raytracing", True)))):
        try:
            setattr(scene.eevee, attr, val)
        except Exception:
            pass
    try:
        scene.view_settings.view_transform = c.get("view_transform", "AgX")
        scene.view_settings.look = c.get("look", "AgX - Punchy")
        scene.view_settings.exposure = float(c.get("exposure", 1.2))
    except Exception:
        pass
    print("P16_LIGHT key=%.1f fill=%.1f rim=%.1f exposure=%.2f sky=%.2f"
          % (c.get("key", 5.0), c.get("fill", 1.1), c.get("rim", 2.0),
             float(c.get("exposure", 1.2)), c.get("sky_strength", 0.75)))
    return merged


def golden_pastoral(scene, cfg=None):
    """装好「金色田园」布光预设（世界、三点光、EEVEE 质量、体积雾、曝光）。"""
    merged = dict(GOLDEN_PASTORAL)
    merged.update(cfg or {})
    golden_lighting(scene, merged)
    return merged


def apply_look(scene, cfg=None, mats=True):
    """worker 渲染入口：程序化材质 + 金色田园布光，一步到位。"""
    out = {}
    if mats:
        out["materials"] = build_materials()
    out["preset"] = golden_pastoral(scene, cfg)
    return out
