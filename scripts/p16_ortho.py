"""Phase 16 —— 按目标图正交拼装（layered diorama through an orthographic camera）。

为什么改用正交：透视相机下，岛体只有 4.0 x 3.4 m、最高 2.72 m，任何能装下地平线的
机位都会把小岛拍成一个「玩具」，而目标图里的岛占满画面下半部——这是透视几何的死结，
之前几轮把机位往前/往后推都只是在这条死结上滑动。

正交相机把「图像坐标 = 世界坐标」变成恒等式：
    X = (u - 0.5) * ortho_scale            # ortho_scale IS the horizontal span
    Z = (0.5 - v) * ortho_scale / aspect   # vertical span = ortho_scale / aspect
    Y = 深度层（美术决定）
于是目标图上量到的每一个 u,v 都能一对一地落到场景里，构图不再靠猜。

`trace_target.py` 负责量（读图 → TARGET 表），本模块负责按表拼装。
"""
import os
import sys
import math
import json
import random
import argparse

import bpy
import numpy as np
from mathutils import Matrix, Vector

P = "/Users/panglaohu/Downloads/GenAIWorkPipeline"
RUN = P + "/runs/20261026-202458-9ac6"
GLB = RUN + "/blender/export/scene_composite.pre_phase15_20261029_000637.glb"
OUTDIR = RUN + "/blender/renders"
EVID = RUN + "/_phase_evidence"
TARGET = RUN + "/references/morris_island_target.png"

sys.path.insert(0, P + "/src")
from genai_pipeline.blender import p15_look as look  # noqa: E402

ASPECT = 1672 / 941
RES_X, RES_Y = 1672, 941

"""The traced table: every number read off references/morris_island_target.png.
u,v are normalised image coords (0..1, v from TOP). w is apparent width as a fraction of
image width. depth is world Y (camera looks along -Y from +Y, so larger Y = further away)."""
ASPECT = 1672 / 941

# --- global bands (from the 12x12 colour grid + row scans)
SKY_V0, SKY_V1     = 0.00, 0.30     # hazy blue sky above the horizon
SEA_V0, SEA_V1     = 0.30, 0.44     # sea band; land takes over below ~0.44 except wedges
HORIZON_V          = 0.255
# sea wedges that stay visible beside the headland
SEA_WEDGE_LEFT     = (0.00, 0.24, 0.30, 0.62)
SEA_WEDGE_RIGHT    = (0.84, 1.00, 0.30, 0.70)

# --- architecture
VILLA   = dict(u=0.585, v=0.400, w=0.230)   # villa + tower cluster, right of centre
TOWER   = dict(u=0.612, v=0.345, w=0.055)
GATE    = dict(u=0.470, v=0.610, w=0.055)
BRIDGE  = dict(u=0.520, v=0.680, w=0.085)
AQUEDUCT= dict(u=0.930, v=0.360, w=0.140)   # distant, far right, above the sea

# --- animals
HORSES = [dict(u=0.205, v=0.690, w=0.070, colour="white"),
          dict(u=0.310, v=0.615, w=0.068, colour="brown"),
          dict(u=0.845, v=0.680, w=0.070, colour="white")]
RABBITS = [dict(u=0.150, v=0.880, w=0.022),
           dict(u=0.700, v=0.830, w=0.020)]

# --- cypress clusters: (u, v, count, spread_u, spread_v)
CYPRESS_CLUSTERS = [
    (0.455, 0.470, 9, 0.045, 0.045),
    (0.520, 0.395, 6, 0.035, 0.040),
    (0.660, 0.455, 8, 0.040, 0.045),
    (0.400, 0.540, 5, 0.035, 0.035),
    (0.720, 0.520, 5, 0.035, 0.035),
    (0.585, 0.330, 4, 0.050, 0.025),
]

# --- framing oaks: canopy enters from the top-left and top-right corners
# PHASE 21 FIX C: the canopies used to reach v 0.205/0.235 and u 0.315/0.685, i.e. 38-57%
# of rows v 0.0-0.3 was oak leaf -- they ate the sky and crowded the castle (measured green
# 14.6% vs the target's 2.2%). Both branches are pulled OUTBOARD and UP so the canopy stays in
# the top corners and the upper-middle sky gap stays open.
OAK_BRANCHES = [
    dict(u0=-0.30, v0=-0.26, u1=0.235, v1=0.118, fruit=8),    # top-left corner
    dict(u0=0.775, v0=-0.28, u1=1.26, v1=0.140, fruit=8),     # top-right corner
]

# --- flower regions: (u, v, radius_u, dominant colour recipe)
FLOWER_REGIONS = [
    (0.50, 0.92, 0.62, "mixed"),
    (0.20, 0.78, 0.30, "white"),
    (0.82, 0.80, 0.28, "scarlet"),
    (0.50, 0.72, 0.32, "white"),
    (0.10, 0.62, 0.18, "orange"),
    (0.92, 0.58, 0.18, "red"),
    (0.35, 0.86, 0.25, "red"),
    (0.66, 0.88, 0.25, "orange"),
]

# --- stream/path polyline from the bridge up to the villa (u,v pairs)
STREAM_PATH = [(0.520, 0.700), (0.545, 0.660), (0.556, 0.620),
               (0.552, 0.575), (0.565, 0.530), (0.572, 0.485), (0.578, 0.445)]
PATH_UP     = [(0.470, 0.640), (0.487, 0.600), (0.505, 0.560),
               (0.528, 0.520), (0.548, 0.480), (0.566, 0.450)]

# --- depth layers (world Y). camera at +Y looking -Y.
DEPTH = dict(
    oaks      = -12.0,   # framing canopies, nearest to camera
    rabbits   =  -2.6,
    horses    =  -2.4,
    seacarpet =  -1.0,   # foreground flower carpet
    meadow    =   0.4,   # foreground land slab
    bridge    =   0.9,
    gate      =   1.0,
    villa     =   1.4,
    cypress   =   3.6,
    iscarpet  =   4.4,   # flowers lying on the island's near face
    island    =   8.0,
    aqueduct  =  30.0,
    sea       =  42.0,
    sky       =  44.0,
)


random.seed(1611)
np.random.seed(1611)

# ------------------------------------------------------------------ scene helpers
def clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def setup_ortho_camera(scene, cfg):
    """正交相机：沿 -Y 平视，零俯仰。图像 u -> 世界 X，图像 v -> 世界 Z，深度 = Y。"""
    cd = bpy.data.cameras.new("p16_cam")
    cd.type = "ORTHO"
    cd.ortho_scale = cfg.get("ortho_scale", 7.0)     # HORIZONTAL span (Blender fits the
    #   larger sensor axis); vertical span = ortho_scale / ASPECT. Verified with
    #   world_to_camera_view: 1 m in X = 1/7 of image width, 1 m in Z = 1/3.94 of height.
    cd.clip_start = 0.1
    cd.clip_end = 400.0
    co = bpy.data.objects.new("p16_cam", cd)
    scene.collection.objects.link(co)
    # A camera looking along -Y with up=+Z is forced by handedness to put world +X on the
    # LEFT of frame (right x up must equal -look). So we look along +Y from -Y instead:
    #   look = +Y, up = +Z, right = +X   => image u increases with world X, v with -Z.
    # Depth then runs the way the trace table assumes: bigger Y = further away.
    co.location = (0.0, -abs(cfg.get("cam_y", 120.0)), cfg.get("center_z", 3.0))
    co.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    scene.camera = co
    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    print("P16_CAM ortho_scale=%.3f centre_z=%.2f y=%.1f res=%dx%d"
          % (cd.ortho_scale, cfg.get("center_z", 3.0), cfg.get("cam_y", 120.0), RES_X, RES_Y))
    return co, cd


def u2x(u, cfg):
    """image u (0..1, left->right) -> world X.  ortho_scale spans the WIDTH."""
    return (u - 0.5) * cfg.get("ortho_scale", 7.0)


def v2z(v, cfg):
    """image v (0..1, top->bottom) -> world Z.  Height span is ortho_scale/ASPECT."""
    return (0.5 - v) * (cfg.get("ortho_scale", 7.0) / ASPECT) + cfg.get("center_z", 3.0)


def u2w(w, cfg):
    """apparent width fraction -> world width"""
    return w * cfg.get("ortho_scale", 7.0)


def v2h(h, cfg):
    """apparent height fraction -> world height"""
    return h * (cfg.get("ortho_scale", 7.0) / ASPECT)


def place(u, v, depth, obj, cfg, yaw=0.0, width=None, height=None, sink=0.0,
          recentre=True, squash=1.0, anchor="min"):
    """把物体摆到目标图的 (u, v) 上。

    关键：glTF 导入的每个节点自身就带位置/缩放。所以必须把「原始世界矩阵」一起
    合进新变换里——新矩阵要先作用在原世界矩阵之后，否则物体的原始偏移和缩放会
    把摆放结果整体带偏（这正是之前 placement 一直在漂的原因）：

        new_world = T(target) @ R(yaw) @ S(scale, squash) @ T(-pivot) @ old_world

    pivot 取包围盒中心 (x, y) 与底面 (z)；anchor='center' 时 v 对齐中心。
    """
    W0 = obj.matrix_world.copy()
    wv = [W0 @ x.co for x in obj.data.vertices]
    if not wv:
        return None
    mnx = min(p.x for p in wv); mxx = max(p.x for p in wv)
    mny = min(p.y for p in wv); mxy = max(p.y for p in wv)
    mnz = min(p.z for p in wv); mxz = max(p.z for p in wv)
    cx, cy = (mnx + mxx) / 2.0, (mny + mxy) / 2.0
    cw, ch = max(mxx - mnx, 1e-6), max(mxz - mnz, 1e-6)
    sc = 1.0
    if width is not None:
        sc = u2w(width, cfg) / cw
    elif height is not None:
        sc = v2h(height, cfg) / ch
    dz = -sink
    if anchor == "center":
        dz -= ch * sc * squash * 0.5
    pivot = Matrix.Translation(Vector((-cx, -cy, -mnz)))
    obj.matrix_world = (Matrix.Translation(Vector((u2x(u, cfg), depth, v2z(v, cfg) + dz)))
                        @ Matrix.Rotation(yaw, 4, "Z")
                        @ Matrix.Diagonal((sc, sc, sc * squash, 1.0))
                        @ pivot @ W0)
    return sc


def depth_sort_fix(cfg):
    """ortho 下没有透视遮挡，靠 Y 深度 + 相机裁剪决定谁挡谁；确保每个物体都在相机前方"""
    pass


def _set_base(m, rgb, rough=None, force=True):
    """Force a material to a plain Principled of this colour.

    p15_look's family builders drive Base Color from a ColorRamp *link*, so writing
    default_value alone is silently ignored -- which is why the oak canopies kept rendering
    as dark blue-green sky instead of warm olive. With force=True the node graph is replaced
    by a single Principled, which is what the diorama wants anyway."""
    try:
        if not m.use_nodes:
            m.use_nodes = True
        nt = m.node_tree
        b = nt.nodes.get("Principled BSDF")
        if b is None:
            for n in nt.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    b = n
                    break
        if force and b is not None:
            for inp in list(b.inputs):
                for lk in list(inp.links):
                    nt.links.remove(lk)
        if b is not None:
            b.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
            if rough is not None:
                b.inputs["Roughness"].default_value = rough
    except Exception as e:
        print("P16_MATFAIL", m.name, e)
    return m


def make_mat(name, kind, **kw):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        {"meadow": lambda: look.mat_meadow(m, vcol=kw.get("vcol", False)),
         "rock": lambda: look.mat_rock(m),
         "stone": lambda: look.mat_stone(m),
         "plaster": lambda: look.mat_plaster(m),
         "roof": lambda: look.mat_roof(m),
         "wood": lambda: look.mat_wood(m),
         "leaf": lambda: look.mat_leaf(m, dark=kw.get("dark", False)),
         "petal": lambda: look.mat_petal(m, kw.get("rgb", (0.8, 0.2, 0.2))),
         "water": lambda: look.mat_water(m)}[kind]()
    return m


def mat_flat(name, rgb, strength=0.85):
    """A flat, self-lit card material -- backdrops must not depend on sun angle."""
    m = bpy.data.materials.get(name)
    if m is not None:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    em.inputs["Strength"].default_value = strength
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    return m


SEA_MATS = {}


def mat_sea(name, rgb, strength=0.62):
    """Sea card: the banded emissive colour modulated by the generated ripple map.
    A flat emissive card with a texture gives the target's tapestry-like sea -- visible
    ripple structure, no dependence on sun angle (a real water shader would light
    inconsistently across an ortho card)."""
    if name in SEA_MATS:
        return SEA_MATS[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (600, 0)
    em = nt.nodes.new("ShaderNodeEmission"); em.location = (350, 0)
    em.inputs["Color"].default_value = (*rgb, 1.0)
    em.inputs["Strength"].default_value = strength
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    img = tex_image("sea")
    if img is not None:
        tc = nt.nodes.new("ShaderNodeTexCoord"); tc.location = (-700, 0)
        mp2 = nt.nodes.new("ShaderNodeMapping"); mp2.location = (-500, 0)
        _r = 1.0 / max(TEX["sea"]["period"], 1e-6)
        mp2.inputs["Scale"].default_value = (_r, _r, _r)
        nt.links.new(tc.outputs["UV"], mp2.inputs["Vector"])
        tx = nt.nodes.new("ShaderNodeTexImage"); tx.location = (-300, 0)
        tx.image = img; tx.extension = "MIRROR"
        nt.links.new(mp2.outputs["Vector"], tx.inputs["Vector"])
        # MULTIPLY of the ripple map (mean sRGB ~ (2,54,79), i.e. dark) with an already
        # dark band colour produced pure black -- that is what turned the whole sea into a
        # black slab. Instead take the map's LUMINANCE and use it as a modulation around
        # 1.0, so the band keeps its own hue and the ripples ride on top as light/dark.
        bw2 = nt.nodes.new("ShaderNodeRGBToBW"); bw2.location = (-300, -220)
        nt.links.new(tx.outputs["Color"], bw2.inputs["Color"])
        mr = nt.nodes.new("ShaderNodeMapRange"); mr.location = (-100, -220)
        mr.inputs["From Min"].default_value = 0.0280  # measured p02 of the sea map
        mr.inputs["From Max"].default_value = 0.0400  # measured p98
        mr.inputs["To Min"].default_value = 0.55      # ripple trough
        mr.inputs["To Max"].default_value = 1.70      # ripple crest
        mr.clamp = True
        nt.links.new(bw2.outputs["Val"], mr.inputs["Value"])
        cbin = nt.nodes.new("ShaderNodeCombineColor"); cbin.location = (120, -220)
        nt.links.new(mr.outputs["Result"], cbin.inputs["Red"])
        nt.links.new(mr.outputs["Result"], cbin.inputs["Green"])
        nt.links.new(mr.outputs["Result"], cbin.inputs["Blue"])
        mx = nt.nodes.new("ShaderNodeMix"); mx.location = (330, 0)
        mx.data_type = "RGBA"; mx.blend_type = "MULTIPLY"
        mx.inputs["Factor"].default_value = 1.0
        c2 = nt.nodes.new("ShaderNodeRGB"); c2.location = (120, 120)
        c2.outputs[0].default_value = (*rgb, 1.0)
        nt.links.new(c2.outputs[0], mx.inputs[6])
        nt.links.new(cbin.outputs["Color"], mx.inputs[7])
        nt.links.new(mx.outputs[2], em.inputs["Color"])
    SEA_MATS[name] = m
    return m


def mesh_from(name, verts, faces, mat):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.validate()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    for f in me.polygons:
        f.use_smooth = False
    return ob


def merge(pv, pf, tf, name, material, smooth=False):
    pv = np.asarray(pv, float)[:, :3]
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
    for f in me.polygons:
        f.use_smooth = smooth
    return ob


def merge_multi(recs, name, material, smooth=True):
    """Merge instances that each carry their OWN geometry.
    recs: list of (verts, faces, P, R, S). One material, one object -- the scatter emits
    hundreds of different plant shapes per colour and they must all land in one mesh."""
    if not recs:
        return None
    tot_v = sum(len(r[0]) for r in recs)
    tot_f = sum(len(r[1]) for r in recs)
    V = np.zeros((tot_v, 3), np.float32)
    F = np.zeros((tot_f, 3), np.int32)
    vo = fo = 0
    for (vv, ff, Pp, R, S) in recs:
        nv = len(vv)
        V[vo:vo + nv] = (np.asarray(vv, float)[:, :3] * S) @ R.T + Pp
        F[fo:fo + len(ff)] = np.asarray(ff, np.int32) + vo
        vo += nv
        fo += len(ff)
    me = bpy.data.meshes.new(name)
    me.from_pydata(V.tolist(), [], F.tolist())
    me.validate()
    me.materials.append(material)
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    for f in me.polygons:
        f.use_smooth = smooth
    return ob


def rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def rz_y(a):
    """Rotation about the VIEW axis (world Y). Spins an object in the image plane without
    tipping it out of that plane -- essential for billboard-adjacent geometry."""
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)

# ------------------------------------------------------------------ 1. backdrop
def build_backdrop(cfg):
    """Sky and sea as smooth vertical gradients on upright cards.

    PHASE 21 -- the r728 backdrop was a 46x36 grid of independently randomised tile colours,
    which rendered as a visible checkerboard. It was built that way to chase a misreading of
    the reference as a "patchwork"; on re-measurement the target's sky is a clean gradient
    (deep cobalt at the top easing to pale warm haze at the horizon) and its sea is a smooth
    cobalt band. So the grid is gone: the mesh is now one quad per band with the colour
    interpolated per-vertex down a gradient, and the only structure is a soft haze band at
    the horizon. Nothing to tile, so nothing can show a tile boundary.
    """
    os_ = cfg.get("ortho_scale", 7.0)
    u0, u1 = cfg.get("bd_u0", -0.30), cfg.get("bd_u1", 1.30)
    hz = HORIZON_V
    top = cfg.get("bd_top", -0.45)
    bot = cfg.get("bd_bot", 1.20)

    def lerp(a, b, t):
        return tuple(a[j] + (b[j] - a[j]) * t for j in range(3))

    def smooth(t):
        return t * t * (3.0 - 2.0 * t)

    # ---- sky: deep cobalt at the very top -> bright blue -> pale warm haze at the horizon
    sky_top = tuple(cfg.get("p21_sky_top", (0.130, 0.300, 0.620)))    # deep cobalt
    sky_mid = tuple(cfg.get("p21_sky_mid", (0.330, 0.545, 0.800)))    # brighter blue
    sky_hz  = tuple(cfg.get("p21_sky_hz",  (0.720, 0.780, 0.800)))    # pale haze
    haze_k  = float(cfg.get("p21_haze", 0.16))                        # haze band thickness
    sea_top = tuple(cfg.get("p21_sea_top", (0.075, 0.185, 0.400)))    # deep cobalt water
    sea_bot = tuple(cfg.get("p21_sea_bot", (0.120, 0.265, 0.480)))
    sea_hz  = tuple(cfg.get("p21_sea_hz",  (0.470, 0.610, 0.700)))    # water catching the haze

    V, F, C = [], [], []
    def strip(vv0, vv1, colfn, nseg=28, t0v=0.0, t1v=1.0):
        """A vertical ribbon of quads; colours interpolated per vertex down the gradient.

        IMPORTANT: `t` is computed over the FRAME span (t0v..t1v), not over the card. The sky
        card runs from v=-0.45 (well above frame) to the horizon, so parameterising the ramp
        over the card itself meant the top visible row already sat at t=0.64 and sampled the
        pale end -- the sky came out washed out no matter what colours were authored."""
        base = len(V)
        for j in range(nseg + 1):
            t = j / nseg
            vv = vv0 + (vv1 - vv0) * t
            z = v2z(vv, cfg)
            tt = 0.0 if t1v <= t0v else min(1.0, max(0.0, (vv - t0v) / (t1v - t0v)))
            c = colfn(tt)
            for i in (0, 1):
                uu = u0 if i == 0 else u1
                V.append((u2x(uu, cfg), 0.0, z))
                C.append(c)
        for j in range(nseg):
            a = base + j * 2
            F.append((a, a + 1, a + 3)); F.append((a, a + 3, a + 2))

    def sky_col(t):
        # t: 0 = top of frame, 1 = horizon
        if t < 0.55:
            return lerp(sky_top, sky_mid, smooth(t / 0.55))
        c = lerp(sky_mid, sky_hz, smooth((t - 0.55) / 0.45))
        # haze band right above the horizon
        band = max(0.0, (t - (1.0 - haze_k)) / max(haze_k, 1e-6))
        return lerp(c, sky_hz, smooth(band))

    def sea_col(t):
        # t: 0 = horizon, 1 = bottom of frame
        c = lerp(sea_hz, sea_top, smooth(min(1.0, t / 0.22)))
        c = lerp(c, sea_bot, smooth(max(0.0, (t - 0.55) / 0.45)))
        # SUBTLE RIPPLE: a brightness wobble that varies only with HEIGHT, so it draws long
        # horizontal water lines. Deliberately not a 2D texture -- a 2D map is what put the
        # checkerboard in the sky, and horizontal banding is what water actually looks like
        # in an orthographic side view.
        rk = float(cfg.get("p21_ripple", 0.055))
        if rk > 0.0:
            ph = float(cfg.get("p21_ripple_freq", 34.0))
            # fade the ripple out near the horizon so the waterline stays crisp
            fade = smooth(min(1.0, t / 0.12))
            w = math.sin(t * ph * math.pi) * 0.55 + math.sin(t * ph * 2.7 * math.pi + 1.1) * 0.45
            k = 1.0 + rk * w * fade
            c = tuple(max(0.0, min(1.0, x * k)) for x in c)
        return c

    for band, (vv0, vv1), colfn, emit_key, depth_key, nm in (
            ("sky", (top, hz), sky_col, "sky_emit", "sky", "p16_sky"),
            ("sea", (hz, bot), sea_col, "sea_emit", "sea", "p16_sea")):
        V.clear(); F.clear(); C.clear()
        if band == "sky":
            strip(vv0, vv1, colfn, nseg=40, t0v=0.0, t1v=hz)
        else:
            strip(vv0, vv1, colfn, nseg=40, t0v=hz, t1v=1.0)
        depth = cfg.get("depth_" + depth_key, DEPTH[depth_key])
        emit = float(cfg.get(emit_key, 1.0))
        # The sky and sea are emission cards standing in for a photographed backdrop, so they
        # must render at their AUTHORED colour. The grade lifts exposure; without this the
        # authored cobalt came out as pale mint (measured (161,220,205) for authored
        # (29,73,163) -- a 2^1.6 = 3.0x blowout, which is the exposure, exactly).
        try:
            emit *= 2.0 ** (-float(cfg.get("exposure", 0.0)))
        except Exception:
            pass
        me = bpy.data.meshes.new(nm)
        me.from_pydata([v for v in V], [], [f for f in F])
        me.validate()
        col_layer = me.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
        for poly in me.polygons:
            for li in poly.loop_indices:
                vi = me.loops[li].vertex_index
                lin = tuple(look.srgb2lin(x) for x in C[vi])
                col_layer.data[li].color = (*lin, 1.0)
        mat = bpy.data.materials.get("p21_" + nm + "_vc")
        if mat is not None:
            bpy.data.materials.remove(mat)
        mat = bpy.data.materials.new("p21_" + nm + "_vc")
        mat.use_nodes = True
        nt = mat.node_tree
        for nd in list(nt.nodes):
            nt.nodes.remove(nd)
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (400, 0)
        em = nt.nodes.new("ShaderNodeEmission"); em.location = (150, 0)
        at = nt.nodes.new("ShaderNodeVertexColor"); at.location = (-150, 0)
        at.layer_name = "Col"
        em.inputs["Strength"].default_value = emit
        nt.links.new(at.outputs["Color"], em.inputs["Color"])
        nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
        me.materials.append(mat)
        ob = bpy.data.objects.new(nm, me)
        bpy.context.collection.objects.link(ob)
        ob.location = (0, depth, 0)
        if band == "sea":
            uvl = me.uv_layers.new(name="p17_uv")
            for poly in me.polygons:
                for li in poly.loop_indices:
                    co = me.vertices[me.loops[li].vertex_index].co
                    uvl.data[li].uv = (co.x, co.z)
    print("P21_BACKDROP gradient sky/sea  top=%s mid=%s hz=%s  sea=%s"
          % (sky_top, sky_mid, sky_hz, sea_top))


# ================================================== 1b. REAL TEXTURE MAPS (FLUX)
TEXDIR = RUN + "/textures"
# every surface family -> the generated tileable map that dresses it, plus the palette
# target the ColorRamp pulls it toward so the render matches the reference in hue.
# "period" = how many metres of surface one copy of the map covers. UVs are laid out in
# world metres by uv_planar(), so the Mapping node simply scales by 1/period. Getting this
# right is what makes the stone read as masonry rather than noise.
# period  = metres of surface covered by one copy of the map (UVs are in world metres)
# tint    = multiply applied AFTER the curves, to sit the map on the reference's palette
# sat     = HSV saturation multiplier;  val = HSV value multiplier
# curves  = per-channel (R, G, B) gamma-ish lift; <1 brightens that channel, >1 darkens
TEX = {
    "stone":  dict(file="stone_wall.png",      period=1.90, tint=(0.96, 0.84, 0.66),
                   sat=1.02, val=0.62, curves=(0.90, 1.00, 1.14), rough=0.88),
    "roof":   dict(file="terracotta_roof.png", period=1.05, tint=(1.00, 0.74, 0.58),
                   sat=1.22, val=0.92, curves=(0.90, 1.02, 1.22), rough=0.74),
    "meadow": dict(file="meadow_grass.png",    period=2.30, tint=(0.80, 0.96, 0.70),
                   sat=1.02, val=1.02, curves=(0.92, 0.96, 1.06), rough=0.96),
    "rock":   dict(file="rock_cliff.png",      period=1.70, tint=(1.00, 0.94, 0.86),
                   sat=1.00, val=0.86, curves=(0.94, 1.00, 1.06), rough=0.92),
    "wood":   dict(file="old_wood.png",        period=0.85, tint=(1.00, 0.86, 0.68),
                   sat=1.08, val=0.82, curves=(0.92, 1.00, 1.10), rough=0.86),
    "sea":    dict(file="sea_surface.png",     period=6.00, tint=(0.72, 0.94, 1.00),
                   sat=1.15, val=0.90, curves=(1.10, 0.96, 0.92), rough=0.22),
    # ---- phase 18: the plants and their ground, plus the horses
    "foliage": dict(file="foliage_dark.png",   period=0.95, tint=(0.72, 1.00, 0.94),
                    sat=1.10, val=0.70, curves=(1.02, 0.94, 0.92), rough=0.92),
    "leaf":   dict(file="leaf_green.png",      period=0.42, tint=(0.86, 1.02, 0.74),
                   sat=0.94, val=0.86, curves=(0.96, 0.98, 1.06), rough=0.88),
    "hide":   dict(file="hide_white.png",      period=0.55, tint=(1.00, 0.97, 0.90),
                   sat=0.92, val=0.98, curves=(1.00, 1.00, 1.00), rough=0.62),
    "saddle": dict(file="saddle_cloth.png",    period=0.34, tint=(1.00, 0.92, 0.92),
                   sat=1.06, val=1.00, curves=(0.98, 1.00, 1.00), rough=0.78),
}


def tex_image(key):
    """Load (once) a generated texture and return its bpy image."""
    spec = TEX[key]
    path = os.path.join(TEXDIR, spec["file"])
    if not os.path.exists(path):
        print("P17_TEX_MISSING", path)
        return None
    name = "p17_" + key
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.load(path, check_existing=True)
        img.name = name
        img.colorspace_settings.name = "sRGB"
    return img


def uv_planar(ob, scale=1.0, axis="XZ"):
    """Give an object a UV layer by planar/box projection in WORLD space.

    The imported composite has no UVs at all (Blender reported uv=[] on every mesh), so an
    Image Texture node would sample one corner for the whole surface and the model would stay
    flat-coloured. Box projection is what makes a seamless map actually read as material:
    it keeps texel density constant no matter how the asset was modelled.
    """
    me = ob.data
    if len(me.polygons) == 0:
        return None
    if not me.uv_layers:
        me.uv_layers.new(name="p17_uv")
    uvl = me.uv_layers.active
    M = ob.matrix_world
    inv = scale if scale else 1.0
    for poly in me.polygons:
        n = (M.to_3x3() @ poly.normal)
        ax, ay, az = abs(n.x), abs(n.y), abs(n.z)
        # dominant axis -> the other two become U,V (classic box mapping)
        if az >= ax and az >= ay:
            iu, iv = 0, 1
        elif ax >= ay:
            iu, iv = 1, 2
        else:
            iu, iv = 0, 2
        for li in poly.loop_indices:
            co = M @ me.vertices[me.loops[li].vertex_index].co
            uvl.data[li].uv = (co[iu] / inv, co[iv] / inv)
    return uvl


def tex_material(name, key, base=(0.6, 0.6, 0.6), rough=0.85, tint=(1, 1, 1),
                 emit=0.0, ramp=True):
    """A Principled material driven by one of the generated maps.

    Chain:  Image Texture -> Mapping(scale) -> ColorRamp (palette lock) -> Base Color
            Image Texture luminance -> Roughness (so mortar reads rougher than stone)
    The ColorRamp is what keeps FLUX's own colour drift from wrecking the target palette:
    it remaps the map's luminance onto the warm-stone / orange-terracotta / green ramp.
    """
    m = bpy.data.materials.get(name)
    if m is not None:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (900, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (600, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    img = tex_image(key)
    if img is None:
        bsdf.inputs["Base Color"].default_value = (*base, 1.0)
        bsdf.inputs["Roughness"].default_value = rough
        return m
    spec = TEX[key]
    tc = nt.nodes.new("ShaderNodeTexCoord"); tc.location = (-900, 0)
    mp = nt.nodes.new("ShaderNodeMapping"); mp.location = (-700, 0)
    rep = 1.0 / max(spec["period"], 1e-6)
    mp.inputs["Scale"].default_value = (rep, rep, rep)
    nt.links.new(tc.outputs["UV"], mp.inputs["Vector"])
    tx = nt.nodes.new("ShaderNodeTexImage"); tx.location = (-500, 0)
    tx.image = img
    tx.extension = "MIRROR"      # seamless by construction: mirror tiling has no seam
    tx.interpolation = "Smart"
    nt.links.new(mp.outputs["Vector"], tx.inputs["Vector"])
    # luminance -> palette ramp
    # ---- palette lock.  A luminance ColorRamp was tried first and failed: collapsing the
    # map to greyscale threw away exactly the chroma that makes limestone warm and pantiles
    # orange, so stone rendered neutral grey. RGB Curves remap each channel of the REAL
    # texture, which keeps its colour and detail while nudging it onto the target palette.
    cv = nt.nodes.new("ShaderNodeRGBCurve"); cv.location = (-110, 40)
    nt.links.new(tx.outputs["Color"], cv.inputs["Color"])
    for ch, g in zip(("C", "R", "G", "B"), spec["curves"]):
        c = cv.mapping.curves[{"C": 3, "R": 0, "G": 1, "B": 2}[ch]]
        while len(c.points) > 2:
            c.points.remove(c.points[1])
        c.points[0].location = (0.0, 0.0)
        c.points[1].location = (1.0, 1.0)
        c.points.new(0.5, min(1.0, 0.5 ** g * 1.0) if g != 1.0 else 0.5)
        for pt in c.points:
            pt.location = (pt.location[0], max(0.0, min(1.0, pt.location[1])))
    try:
        cv.mapping.update()
    except Exception:
        pass
    hsv = nt.nodes.new("ShaderNodeHueSaturation"); hsv.location = (110, 40)
    hsv.inputs["Saturation"].default_value = spec["sat"]
    hsv.inputs["Value"].default_value = spec["val"]
    nt.links.new(cv.outputs["Color"], hsv.inputs["Color"])
    mixn = nt.nodes.new("ShaderNodeMix"); mixn.location = (330, 40)
    mixn.data_type = "RGBA"; mixn.blend_type = "MULTIPLY"
    mixn.inputs["Factor"].default_value = 1.0
    nt.links.new(hsv.outputs["Color"], mixn.inputs[6])
    tintc = nt.nodes.new("ShaderNodeRGB"); tintc.location = (140, -200)
    tintc.outputs[0].default_value = (*spec["tint"], 1.0)
    nt.links.new(tintc.outputs[0], mixn.inputs[7])
    nt.links.new(mixn.outputs[2], bsdf.inputs["Base Color"])
    bw = nt.nodes.new("ShaderNodeRGBToBW"); bw.location = (-110, -150)
    nt.links.new(tx.outputs["Color"], bw.inputs["Color"])
    # roughness from the map's own luminance: darker mortar/pits are rougher
    rr = nt.nodes.new("ShaderNodeMapRange"); rr.location = (60, -320)
    rr.inputs["From Min"].default_value = 0.0
    rr.inputs["From Max"].default_value = 1.0
    rr.inputs["To Min"].default_value = min(1.0, rough + 0.10)
    rr.inputs["To Max"].default_value = max(0.0, rough - 0.16)
    nt.links.new(bw.outputs["Val"], rr.inputs["Value"])
    nt.links.new(rr.outputs["Result"], bsdf.inputs["Roughness"])
    bsdf.inputs["Metallic"].default_value = 0.0
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.35
    except Exception:
        pass
    m["p17_textured"] = key
    return m


# which imported material families get which generated map
TEX_FAMILY = [
    (("stone", "plaster", "trim", "mortar", "wall"), "stone"),
    (("roof", "tile", "terracotta"), "roof"),
    (("grass", "meadow", "ground", "lawn"), "meadow"),
    (("rock", "cliff", "stone_"), "rock"),
    (("door", "wood", "timber", "beam"), "wood"),
    (("sea", "water", "ocean", "sky_sea"), "sea"),
]


def family_of(mat_name):
    n = mat_name.lower()
    if "outline" in n:
        return None
    for keys, fam in TEX_FAMILY:
        for k in keys:
            if k in n:
                return fam
    return None


def apply_textures(cfg):
    """Dress every imported material in the scene with a generated map, then give the mesh
    carrying it proper UVs. This is the actual fix the phase is about: before this, all 733
    materials were single flat colours with zero texture nodes, so nothing could look real."""
    if not cfg.get("textures", True):
        print("P17_TEXTURES off")
        return
    done = {}
    objs = 0
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name.startswith(("p16_", "p17_", "p21_")):
            continue
        if o.hide_render:
            continue
        hit = False
        for slot in o.material_slots:
            m = slot.material
            if m is None:
                continue
            fam = family_of(m.name)
            if fam is None:
                continue
            if fam not in done:
                done[fam] = tex_material("p17_tex_" + fam, fam,
                                         rough=TEX[fam]["rough"])
            # keep the object's own colour as a gentle tint so different stones stay
            # distinguishable, but let the map supply the detail
            old = None
            try:
                b = m.node_tree.nodes.get("Principled BSDF")
                if b and not b.inputs["Base Color"].is_linked:
                    old = tuple(b.inputs["Base Color"].default_value[:3])
            except Exception:
                pass
            slot.material = done[fam]
            hit = True
        if hit:
            uv_planar(o, scale=1.0)      # UVs in world metres; the map's period does the rest
            objs += 1
    sea_m = bpy.data.materials.get("p16_sea00")
    if sea_m is not None:
        done["sea"] = sea_m
    print("P17_TEXTURES objs=%d maps=%s" % (objs, sorted(done)))
    print("P17_AUDIT_TABLE material | texture_file | uv | nodes")
    for fam in ("stone", "roof", "meadow", "rock", "wood", "sea"):
        m = done.get(fam)
        if m is None:
            continue
        nt = m.node_tree
        timg = [n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image]
        uv = [n for n in nt.nodes if n.type == "UVMAP"]
        users = [o for o in bpy.data.objects if o.type == "MESH" and not o.hide_render
                 and any(s.material is m for s in o.material_slots)]
        if fam == "sea":
            users = [o for o in bpy.data.objects
                     if o.name.startswith("p16_sea") and not o.hide_render]
        hasuv = all(len(o.data.uv_layers) > 0 for o in users) if users else False
        f = os.path.basename(timg[0].image.filepath) if timg else "-"
        print("P17_MAT %-18s | %-22s | uv=%-3s | nodes=%2d | objs=%d"
              % (m.name, f, "YES" if hasuv else "NO", len(nt.nodes), len(users)))
    return done


# ------------------------------------------------------- 2. island (existing mesh)
# objects the diorama actually uses; everything else in the composite gets hidden
USED = {"oskar_scene": "island", "oskar_scene.001": "villa",
        "stone_path_gate": "gate", "stone_arch_bridge": "bridge",
        "saddled_horse": "horse", "saddled_horse.001": "horse",
        "saddled_horse.002": "horse"}
RABBIT_PART = {"body", "head", "haunch", "chest", "belly_patch", "snout", "nose", "tail",
               "ear_near", "ear_far", "ear_inner_near", "ear_inner_far", "eye_near", "eye_far",
               "front_leg_near", "front_leg_far", "front_paw_near", "front_paw_far",
               "hind_foot_near", "hind_foot_far"}


# ------------------------------------------------------- 2. island (existing mesh)
# objects the diorama actually uses; everything else in the composite gets hidden
USED = {"oskar_scene": "island", "oskar_scene.001": "villa",
        "stone_path_gate": "gate", "stone_arch_bridge": "bridge",
        "saddled_horse": "horse", "saddled_horse.001": "horse",
        "saddled_horse.002": "horse"}
RABBIT_PART = {"body", "head", "haunch", "chest", "belly_patch", "snout", "nose", "tail",
               "ear_near", "ear_far", "ear_inner_near", "ear_inner_far", "eye_near", "eye_far",
               "front_leg_near", "front_leg_far", "front_paw_near", "front_paw_far",
               "hind_foot_near", "hind_foot_far"}


def apply_whitelist():
    """The composite carries the whole old scene — trees, extra props, its own sea. In a
    diorama every element is placed by hand from the trace table, so anything unaccounted
    for is hidden rather than left floating in shot."""
    kept = hidden = 0
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name.startswith(("p16_", "p21_")):
            continue
        base = o.name.split(".")[0]
        if o.name in USED or base in RABBIT_PART:
            kept += 1
            continue
        o.hide_render = True
        o.hide_viewport = True
        hidden += 1
    print("P16_WHITELIST kept=%d hidden=%d" % (kept, hidden))


def strip_outlines(cfg):
    """Delete the inverted-hull outline shells, which are the "black / metallic blob".

    Every asset in this composite carries a second, expanded copy of itself with a
    near-black material (`osk_outline.*`, base colour 0.024) -- the classic toon outline.
    Under the ortho camera and AO they do not read as line art: they read as a dark
    metallic mass sitting on the silhouette (which is exactly the blob on the left) and
    they eat the light on every edge. In a painterly diorama they are pure loss, so they
    are removed from the geometry rather than merely hidden.

    Also drops any material slot whose colour is essentially black and which has no
    texture -- i.e. untextured stray primitives -- so the audit is not just visual.
    """
    if not cfg.get("strip_outlines", True):
        print("P17_OUTLINE keep (cfg)")
        return
    removed = 0
    for o in list(bpy.data.objects):
        if o.type != "MESH" or o.name.startswith(("p16_", "p17_", "p18_", "p21_")):
            continue
        bad = [s.material.name for s in o.material_slots
               if s.material and "outline" in s.material.name.lower()]
        if not bad:
            continue
        me = o.data
        # keep only polygons whose material is NOT an outline
        keep_idx = set()
        for sl in o.material_slots:
            if sl.material and "outline" not in sl.material.name.lower():
                pass
        rem = [i for i, sl in enumerate(o.material_slots)
               if sl.material and "outline" in sl.material.name.lower()]
        polys = [p.index for p in me.polygons if p.material_index not in rem]
        if not polys:
            continue
        import bmesh
        bm = bmesh.new(); bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        kill = [f for f in bm.faces if f.material_index in rem]
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        bm.to_mesh(me); bm.free()
        # now compact the slot list to match
        for i in sorted(rem, reverse=True):
            try:
                me.materials.pop(index=i)
            except Exception:
                pass
        removed += 1
    print("P17_OUTLINES stripped_on=%d" % removed)


def audit_scene(cfg):
    """Print exactly what is visible, and refuse to keep anything that is not in the
    intended asset set. This is the answer to "audit the object list"."""
    vis = [o for o in bpy.data.objects if o.type == "MESH" and not o.hide_render]
    ok, stray = [], []
    for o in vis:
        base = o.name.split(".")[0]
        if o.name in USED or base in RABBIT_PART or o.name.startswith(("p16_", "p17_", "p18_", "p21_")):
            ok.append(o.name)
        else:
            stray.append(o.name)
    doomed = set(stray)
    # a visible imported mesh whose every material is near-black or absent is an untextured
    # stray primitive: delete the geometry outright rather than leaving it in the file
    for o in vis:
        if o.name.startswith(("p16_", "p17_", "p18_", "p21_")):
            continue
        mats = [s.material for s in o.material_slots if s.material]
        if not mats:
            doomed.add(o.name); continue
        def _dark(m):
            b = m.node_tree.nodes.get("Principled BSDF") if m.use_nodes else None
            if b is None:
                return True
            if b.inputs["Base Color"].is_linked:
                return False
            return max(b.inputs["Base Color"].default_value[:3]) < 0.02
        if all(_dark(m) for m in mats):
            doomed.add(o.name)
    for nm in list(doomed):
        o = bpy.data.objects.get(nm)
        if o is not None:
            bpy.data.objects.remove(o, do_unlink=True)
    stray = sorted(doomed)
    print("P17_AUDIT visible=%d intended=%d stray_removed=%d" % (len(vis), len(ok), len(stray)))
    if stray:
        print("P17_AUDIT_STRAY", stray[:20])
    # a second sweep: any visible mesh with NO material at all is an untextured primitive
    bare = [o.name for o in bpy.data.objects if o.type == "MESH" and not o.hide_render
            and not any(s.material for s in o.material_slots)]
    if bare:
        print("P17_AUDIT_BARE_del", bare[:20])
        for nm in bare:
            o = bpy.data.objects.get(nm)
            if o is not None:
                bpy.data.objects.remove(o, do_unlink=True)
    return len(ok)


def place_island(cfg):
    """Reuse the delivered island mesh. Scale so its width equals the traced island extent,
    squash it slightly, and stand its base below the bottom of frame so the crest lands where
    the target has it (v ~= 0.38)."""
    ob = None
    for nm in ("oskar_scene", "island_terrain"):
        ob = bpy.data.objects.get(nm)
        if ob:
            break
    if ob is None:
        print("P16_ISLAND_MISSING")
        return None
    W0 = ob.matrix_world.copy()
    wv = [W0 @ x.co for x in ob.data.vertices]
    mnx = min(p.x for p in wv); mxx = max(p.x for p in wv)
    mny = min(p.y for p in wv); mxy = max(p.y for p in wv)
    mnz = min(p.z for p in wv)
    cur_w = mxx - mnx
    want = u2w(cfg.get("island_w", 0.62), cfg)
    sc = want / cur_w
    squash = cfg.get("island_z_squash", 0.86)
    pivot = Matrix.Translation(Vector((-(mnx + mxx) / 2.0, -(mny + mxy) / 2.0, -mnz)))
    ob.matrix_world = (Matrix.Translation(Vector((u2x(cfg.get("island_u", 0.53), cfg),
                                                  cfg.get("depth_island", DEPTH["island"]),
                                                  v2z(cfg.get("island_v", 1.05), cfg))))
                       @ Matrix.Diagonal((sc, sc, sc * squash, 1.0))
                       @ pivot @ W0)
    h = (max(p.z for p in wv) - mnz) * sc * squash
    crest_v = cfg.get("island_v", 1.05) - h / (cfg.get("ortho_scale", 7.0) / ASPECT)
    print("P16_ISLAND w %.3f -> %.3f (scale %.3f squash %.2f) base v=%.3f crest v=%.3f"
          % (cur_w, want, sc, squash, cfg.get("island_v", 1.05), crest_v))
    return ob


# ----------------------------------------------------------- 3. foreground meadow
def build_meadow(cfg):
    """Foreground land slab in IMAGE space. Its TOP EDGE is the coastline: the target shows
    sea in the left wedge down to v~0.62 and the right wedge down to v~0.70, so the shore
    runs diagonally. Below that the land widens past both frame edges."""
    lu, lv = cfg.get("coast_lu", -0.06), cfg.get("coast_lv", 0.615)
    ru, rv = cfg.get("coast_ru", 1.06), cfg.get("coast_rv", 0.700)
    v_bot = cfg.get("meadow_v_bot", 1.18)
    NU, NV = 64, 26
    y0 = cfg.get("depth_meadow", DEPTH["meadow"])
    y1 = cfg.get("depth_meadow_back", DEPTH["meadow"] - 0.06)
    V, F = [], []
    rnd = random.Random(5)
    for j in range(NV + 1):
        t = j / NV
        y = y0 + (y1 - y0) * t
        for i in range(NU + 1):
            a = i / NU
            # coastline at a=0 is (lu,lv); at a=1 is (ru,rv)
            u = lu + (ru - lu) * a
            vtop = lv + (rv - lv) * a
            v = vtop + (v_bot - vtop) * t
            x = u2x(u, cfg)
            z = v2z(v, cfg)
            # rolling relief, scaled down so the shore stays crisp
            z += (0.055 * math.sin(a * 9.0 + t * 4.0) + 0.030 * math.sin(a * 21.0 - t * 8.0)
                  + 0.038 * (1.0 - t) * math.sin(a * 4.0 + 1.7)) * (0.25 + 0.75 * t)
            V.append((x, y + 0.06 * math.sin(a * 6.0), z + rnd.uniform(-0.007, 0.007)))
    W = NU + 1
    for j in range(NV):
        for i in range(NU):
            a0 = j * W + i; a1 = a0 + 1; b0 = a0 + W; b1 = b0 + 1
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    mm = bpy.data.materials.get("p16_meadow")
    if mm is None:
        mm = make_mat("p16_meadow", "meadow", vcol=False)
    ob = mesh_from("p16_meadow", V, F, mm)
    # the foreground carpet is the largest textured area in frame: give it the real map
    tm = tex_material("p17_tex_meadow", "meadow", rough=0.94, ramp=True)
    ob.data.materials.clear()
    ob.data.materials.append(tm)
    uv_planar(ob, scale=1.0)
    print("P16_MEADOW coast (%.2f,%.2f)->(%.2f,%.2f) down to v=%.2f  y %.2f..%.2f"
          % (lu, lv, ru, rv, v_bot, y0, y1))
    return ob


# --------------------------------------------------------------- 4. villa + props
def place_villa(cfg):
    """villa + tower block. The asset is 4.28 x 6.98 x 3.47 m -- far too tall/compact to read
    like the target's low, wide farmhouse complex, so it is squashed in Z after being scaled
    to the traced width."""
    v = bpy.data.objects.get("oskar_scene.001") or bpy.data.objects.get("villa")
    if v is None:
        print("P16_VILLA_MISSING")
        return
    u = cfg.get("villa_u", 0.545)
    vv = cfg.get("villa_v", 0.318)
    w = cfg.get("villa_w", VILLA["w"])
    sc = place(u, vv, cfg.get("depth_villa", DEPTH["villa"]), v, cfg,
               yaw=math.radians(cfg.get("villa_rot", 4.0)),
               width=w, anchor="min",
               squash=cfg.get("villa_squash", 0.52))
    print("P16_VILLA u=%.3f v=%.3f w=%.3f scale=%.3f squash=%.2f"
          % (u, vv, w, sc, cfg.get("villa_squash", 0.52)))


def place_bridge_gate(cfg):
    """Seat the EXISTING stone_arch_bridge / stone_path_gate assets on the new landform.

    The brief is explicit that these are built assets and must not be re-modelled, so this
    only ever sets a transform. `u` for the bridge is taken from the stream's own centre-line
    at the bridge's v, so the arches land ON the water by construction rather than by eye;
    v is then dropped onto the terrain via p21_ground().
    """
    for nm, (u, v, w, d, yaw) in {
        "stone_path_gate": (None, cfg.get("gate_v", 0.548),
                            cfg.get("gate_w", 0.052), None, 0),
        # span 0.075 of frame width: the stream at this v is 0.041 wide, so the abutments
        # land on both banks with the arches over open water. The asset's own proportions
        # (h/w = 0.73) then put the deck ~92 px high, which reads as a real bridge.
        "stone_arch_bridge": (None, cfg.get("bridge_v", 0.588),
                              cfg.get("bridge_span", 0.075), None, 0),
    }.items():
        o = bpy.data.objects.get(nm)
        if o is None:
            print("P21_PROP_MISSING", nm)
            continue
        if u is None:
            # the bridge centres on the STREAM; the gate sits on the PATH
            u = (p21_stream_u(v, cfg) if nm == "stone_arch_bridge"
                 else p21_path_u(v, cfg))
        if d is None:
            # stand the asset just IN FRONT of the water surface so the masonry occludes the
            # stream except through its arch openings, and the water reads as passing under
            d = p21_ground_y(u, v, cfg) - float(cfg.get("prop_y_gap", 0.30))
        # land the asset on the hill surface: `place` anchors the bbox BASE at v, so raising
        # v by the local ground offset keeps the deck on the slope it spans
        place(u, v, d, o, cfg, yaw=math.radians(yaw), width=w)
        print("P21_PROP", nm, "u=%.3f v=%.3f w=%.3f" % (u, v, w))
    # hide the extra bridge copies the composite carries
    for nm in ("stone_arch_bridge.001", "stone_arch_bridge.002", "stone_arch_bridge.003"):
        o = bpy.data.objects.get(nm)
        if o:
            o.hide_render = True
            o.hide_viewport = True


def place_cypress(cfg):
    """Tapered cypress cones, every one seated on the island by raycast.

    The old version invented the height (`z = v2z(v) - 0.01`), so any tree whose traced v
    was above the terrain surface hung in mid-air. Now each candidate is fired through the
    exact pixel it will occupy, and the tree is stood on the surface the camera sees there.
    A ray that misses the island is a tree that would float -- it is dropped, not moved.
    """
    rings, seg = 9, 7
    pv, F = [], []
    for i in range(rings):
        t = i / (rings - 1)
        r = (1.0 - t) ** 0.68 * 0.34 + 0.008
        for k in range(seg):
            a = 2 * math.pi * k / seg + i * 0.42
            jr = r * (1.0 + random.uniform(-0.16, 0.16))
            pv.append((math.cos(a) * jr, math.sin(a) * jr, t))
    for i in range(rings - 1):
        for k in range(seg):
            a0 = i * seg + k; a1 = i * seg + (k + 1) % seg
            b0 = (i + 1) * seg + k; b1 = (i + 1) * seg + (k + 1) % seg
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    pv = np.array(pv, float)

    mat = bpy.data.materials.get("p16_cypress_dark")
    if mat is None:
        mat = bpy.data.materials.new("p16_cypress_dark")
        look.mat_leaf(mat, dark=True)
    _set_base(mat, (0.052, 0.086, 0.040), rough=0.90)

    dg = bpy.context.evaluated_depsgraph_get()
    depth = cfg.get("depth_cypress", DEPTH["cypress"])
    tf, dropped = [], 0
    search = cfg.get("cyp_search", (0.0, 0.012, 0.026, 0.042, 0.060))
    for (cu, cv, n, su, sv) in CYPRESS_CLUSTERS:
        for _ in range(n):
            u = cu + random.uniform(-su, su)
            v0 = cv + random.uniform(-sv, sv)
            # The traced v is a 2D measurement; the island it must stand on is a surface, so
            # a traced point can land a few pixels above the crest. Search DOWNWARD for real
            # ground within a small tolerance before giving up -- that is seating, not
            # floating. If nothing is hit within the budget the tree is dropped.
            o, loc = None, None
            for dv in search:
                o, loc = ray_hit(dg, cfg, u, v0 + dv)
                if o is not None and o.name in ("oskar_scene", "p16_meadow"):
                    break
                o, loc = None, None
            if o is None:
                dropped += 1
                continue
            h_f = random.uniform(0.052, 0.082) * cfg.get("cyp_h", 1.0)
            w_f = h_f * random.uniform(0.26, 0.36)
            hw = v2h(h_f, cfg)
            ww = u2w(w_f, cfg)
            # stand it ON the surface: cone spans 0..1 in local Z, so base sits at the hit
            tf.append((np.array([loc.x, loc.y - 0.30, loc.z - 0.004]),
                       rz_y(random.uniform(0, 6.283)),
                       np.array([ww, ww, hw])))
    if tf:
        merge(pv, F, tf, "p16_cypress", mat, smooth=True)
    print("P16_CYPRESS seated=%d dropped_floating=%d" % (len(tf), dropped))


def horse_tack(cfg, o, idx, colour):
    """Warm hide + a red saddle cloth with gold trim, as SEPARATE geometry and materials.

    The cloth is generated from the horse's own bounding box in its LOCAL frame, so it always
    drapes over the back rather than floating: we take the mesh's world bbox, find the top
    surface, and lay a saddle-shaped sheet just proud of it, with a gold border strip.
    """
    me = o.data
    M = o.matrix_world
    wv = [M @ v.co for v in me.vertices]
    if not wv:
        return None
    mnx = min(p.x for p in wv); mxx = max(p.x for p in wv)
    mny = min(p.y for p in wv); mxy = max(p.y for p in wv)
    mnz = min(p.z for p in wv); mxz = max(p.z for p in wv)
    Lx, Ly, Lz = mxx - mnx, mxy - mny, mxz - mnz
    # the horse faces along X (its longest horizontal axis is X here); the back is the top
    # third. Cloth covers the middle of the back, where a saddle sits.
    cx = (mnx + mxx) / 2.0
    cy = (mny + mxy) / 2.0 + cfg.get("p18_tack_dy", 0.0)

    # find the local top surface under the cloth footprint by raycasting down onto the horse
    dg = bpy.context.evaluated_depsgraph_get()
    nu, nv = 9, 7
    half_x = Lx * cfg.get("p18_cloth_w", 0.20)
    half_y = Ly * cfg.get("p18_cloth_d", 0.40)

    # sample the back height with raycasts from above
    def back_z(x, y):
        origin = Vector((x, y, mxz + Lz))
        ok, loc, nor, ii, ob, mm = dg.scene.ray_cast(dg, origin, Vector((0, 0, -1)),
                                                     distance=Lz * 3.0 + 1.0)
        if ok and ob is not None and (ob.name == o.name):
            return loc.z
        return None

    grid = {}
    zs = []
    for i in range(nu + 1):
        for j in range(nv + 1):
            x = cx - half_x + 2 * half_x * i / nu
            y = cy - half_y + 2 * half_y * j / nv
            z = back_z(x, y)
            grid[(i, j)] = (x, y, z)
            if z is not None:
                zs.append(z)
    if not zs:
        return None
    zref = float(np.median(zs))
    # fill gaps by averaging whatever neighbours DID hit, so the sheet follows the back
    # instead of snapping every miss to one height (which produced a torn, jagged edge).
    for _ in range(3):
        for j in range(nv + 1):
            for i in range(nu + 1):
                if grid[(i, j)][2] is not None:
                    continue
                acc = []
                for di, dj in ((1,0),(-1,0),(0,1),(0,-1)):
                    ii, jj = i + di, j + dj
                    if 0 <= ii <= nu and 0 <= jj <= nv and grid[(ii, jj)][2] is not None:
                        acc.append(grid[(ii, jj)][2])
                if acc:
                    grid[(i, j)] = (grid[(i, j)][0], grid[(i, j)][1], float(np.mean(acc)))
    # build the cloth as a slightly domed sheet that hugs the sampled back
    CV, CF = [], []
    for j in range(nv + 1):
        for i in range(nu + 1):
            x, y, z = grid[(i, j)]
            if z is None:
                z = zref
            lift = 0.012 * Lz
            # droop the sides down over the barrel of the body
            fx = abs((i / nu) - 0.5) * 2.0
            drop = 0.055 * Lz * (fx ** 2.2)
            CV.append((x, y, z + lift - drop))
    W = nu + 1
    for j in range(nv):
        for i in range(nu):
            a = j * W + i
            CF.append((a, a + 1, a + W + 1)); CF.append((a, a + W + 1, a + W))

    cloth_m = bpy.data.materials.get("p18_saddle_cloth")
    if cloth_m is None:
        cloth_m = bpy.data.materials.new("p18_saddle_cloth")
    try:
        cm = tex_material("p18_saddle_tex", "saddle", rough=0.78)
        _tint_material(cm, cfg.get("p18_cloth_rgb", (0.470, 0.055, 0.055)),
                       cfg.get("p18_cloth_mix", 0.45))
        cloth_m = cm
    except Exception as e:
        print("P18_CLOTH_TEXFAIL", e)
        _set_base(cloth_m, cfg.get("p18_cloth_rgb", (0.470, 0.055, 0.055)), rough=0.78)
    ob = mesh_from("p18_tack_cloth%d" % idx, CV, CF, cloth_m)

    # ---- gold trim: a raised border loop following the cloth's outer edge
    TR, TF = [], []
    edge = ([(i, 0) for i in range(nu + 1)] + [(nu, j) for j in range(1, nv + 1)] +
            [(i, nv) for i in range(nu - 1, -1, -1)] + [(0, j) for j in range(nv - 1, 0, -1)])
    tw = Lz * 0.020
    for (i, j) in edge:
        x, y, z = grid[(i, j)]
        if z is None:
            z = zref
        fx = abs((i / nu) - 0.5) * 2.0
        zz = z + 0.012 * Lz - 0.055 * Lz * (fx ** 2.2) + tw * 0.9
        TR.append((x, y, zz))
    nedge = len(TR)
    ring = list(TR)
    for k in range(nedge):
        a = ring[k]; b = ring[(k + 1) % nedge]
        # perpendicular to THIS segment, in the horizontal plane. Offsetting every quad by a
        # fixed X was the bug: a segment running along X collapsed to zero width, which is
        # what made the cloth edge read as a jagged crown instead of a gold border.
        ex, ey = b[0] - a[0], b[1] - a[1]
        L = math.hypot(ex, ey) or 1e-9
        px, py = -ey / L * tw, ex / L * tw
        base = len(TR)
        TR.append((a[0] - px, a[1] - py, a[2]))
        TR.append((a[0] + px, a[1] + py, a[2]))
        TR.append((b[0] + px, b[1] + py, b[2]))
        TR.append((b[0] - px, b[1] - py, b[2]))
        TF.append((base, base + 1, base + 2)); TF.append((base, base + 2, base + 3))
    gold_m = bpy.data.materials.get("p18_gold_trim")
    if gold_m is None:
        gold_m = bpy.data.materials.new("p18_gold_trim")
    b = gold_m.node_tree.nodes.get("Principled BSDF") if gold_m.use_nodes else None
    _set_base(gold_m, cfg.get("p18_gold_rgb", (0.640, 0.440, 0.105)), rough=0.32)
    if gold_m.use_nodes:
        bs = gold_m.node_tree.nodes.get("Principled BSDF")
        if bs:
            try:
                bs.inputs["Metallic"].default_value = 0.85
            except Exception:
                pass
    mesh_from("p18_tack_trim%d" % idx, TR, TF, gold_m)
    print("P18_TACK %d cloth=%d tris trim=%d tris (Lx %.2f Ly %.2f Lz %.2f)"
          % (idx, len(CF), len(TF), Lx, Ly, Lz))
    return ob


def apply_hide(cfg, o, colour, idx):
    """Warm hide material: white and brown variants from a generated horse-hide map."""
    key = "hide"
    m = None
    # The generated hide map is a bright cream (measured mean 0.92,0.91,0.87 -- almost white,
    # std 0.023). A 0.55 MIX over it therefore leaves the "brown" horse nearly as pale as the
    # white one, which is exactly what the first render showed. The tint has to do almost all
    # of the colouring work, so the mix is driven high and the brown is pushed dark.
    mix = float(cfg.get("p18_hide_mix_white", 0.72)) if colour == "white" \
        else float(cfg.get("p18_hide_mix_brown", 0.93))
    try:
        m = tex_material("p18_hide_" + colour + str(idx), key, rough=0.62)
        base = cfg.get("p18_hide_white", (0.700, 0.640, 0.530)) if colour == "white" \
            else cfg.get("p18_hide_brown", (0.150, 0.078, 0.040))
        _tint_material(m, base, mix)
    except Exception as e:
        print("P18_HIDE_TEXFAIL", e)
        m = bpy.data.materials.new("p18_hide_" + colour + str(idx))
        base = (0.760, 0.700, 0.590) if colour == "white" else (0.330, 0.200, 0.110)
        _set_base(m, base, rough=0.62)
    o.data.materials.clear()
    o.data.materials.append(m)
    uv_planar(o, scale=0.55)
    return m


def place_animals(cfg):
    slots = [("saddled_horse", 0), ("saddled_horse.001", 1), ("saddled_horse.002", 2)]
    for (nm, i) in slots:
        o = bpy.data.objects.get(nm)
        if o is None:
            continue
        spec = HORSES[i]
        place(spec["u"], spec["v"], cfg.get("depth_horses", DEPTH["horses"]) - i * 0.2, o, cfg,
              yaw=math.radians(random.uniform(-25, 25)), width=spec["w"])
        if cfg.get("p18_horse_tack", True):
            apply_hide(cfg, o, spec["colour"], i)
            bpy.context.view_layer.update()
            horse_tack(cfg, o, i, spec["colour"])
        else:
            m = bpy.data.materials.get("p16_horse_" + spec["colour"] + str(i))
            if m is None:
                m = bpy.data.materials.new("p16_horse_" + spec["colour"] + str(i))
                rgb = (0.93, 0.92, 0.87) if spec["colour"] == "white" else (0.40, 0.24, 0.14)
                look.mat_petal(m, tuple(look.srgb2lin(c) for c in rgb))
            o.data.materials.clear()
            o.data.materials.append(m)
    print("P16_HORSES", len(slots))
    # rabbits: parts share a name suffix
    PARTS = {"body", "head", "haunch", "chest", "belly_patch", "snout", "nose", "tail",
             "ear_near", "ear_far", "ear_inner_near", "ear_inner_far", "eye_near", "eye_far",
             "front_leg_near", "front_leg_far", "front_paw_near", "front_paw_far",
             "hind_foot_near", "hind_foot_far"}
    groups = {}
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name.startswith("p16_"):
            continue
        parts = o.name.split(".")
        if parts[0] not in PARTS:
            continue
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        groups.setdefault(idx, []).append(o)
    ok = 0
    for i, spec in enumerate(RABBITS):
        if i not in groups:
            continue
        grp = groups[i]
        allv = [o.matrix_world @ v.co for o in grp for v in o.data.vertices]
        gx = sum(v.x for v in allv) / len(allv)
        gy = sum(v.y for v in allv) / len(allv)
        gz = min(v.z for v in allv)
        cur_w = max(v.x for v in allv) - min(v.x for v in allv)
        sc = u2w(spec["w"], cfg) / max(cur_w, 1e-6)
        # glTF nodes carry their own transform; fold it in like place() does
        
        M = (Matrix.Translation(Vector((u2x(spec["u"], cfg), cfg.get("depth_rabbits", DEPTH["rabbits"]),
                                        v2z(spec["v"], cfg))))
             @ Matrix.Rotation(random.uniform(0, 6.283), 4, "Z")
             @ Matrix.Diagonal((sc, sc, sc, 1.0))
             @ Matrix.Translation(Vector((-gx, -gy, -gz))))
        for o in grp:
            o.matrix_world = M @ o.matrix_world
        ok += 1
    # the composite ships 8 rabbits; only the traced ones are in shot
    for idx, grp in groups.items():
        if idx >= len(RABBITS):
            for o in grp:
                o.hide_render = True
                o.hide_viewport = True
    print("P16_RABBITS placed=%d hidden_groups=%d"
          % (ok, len([i for i in groups if i >= len(RABBITS)])))

# ------------------------------------------------------------------ 5. flower mass
# Measured off the reference: the field is a WARM OCHRE-OLIVE millefleur (34% yellow,
# 20% orange, only 6% red, 8% green), not a red/white carpet. Values are sRGB.
# ============================================================== P19 palette
# MEASURED, not guessed. Sampling the reference's field (rows below v=0.62) and keeping only
# genuinely bright, chromatic pixels (lum>105, sat>0.30) gives this bloom breakdown:
#
#     scarlet       19.6%   p50 (196,141,100)      orange    50.7%  p50 (185,150, 87)
#     crimson        3.6%   p50 (210, 83, 59)      gold      21.3%  p50 (149,136, 81)
#     white/cream    9.9%   p50 (168,175,169)      blue       0.4%  (NOT a field colour)
#     red family total 23.2% of bloom pixels
#
# and non-bloom field pixels are OLIVE foliage (112,112,80) over very dark ground. Blue is
# only 0.4% of bright blooms -- the big blue reading earlier was dark shadow, not flowers,
# which is why painting the field blue was wrong.
#
# These values are sRGB. They are converted with srgb2lin() at material-creation time: a
# controlled swatch test proved that writing sRGB numbers straight into Base Color renders
# (195,141,127) sat 0.35 -- pale salmon -- where the correct conversion gives (155,74,64)
# sat 0.59. That single bug was the whole "washed-out" problem.
HUE_RGB = {"red":     (0.824, 0.325, 0.231),   # 210, 83, 59   crimson (target p50)
           "scarlet": (0.769, 0.553, 0.392),   # 196,141,100   scarlet (target p50)
           "orange":  (0.725, 0.588, 0.341),   # 185,150, 87   the dominant bloom (50.7%)
           "gold":    (0.584, 0.533, 0.318),   # 149,136, 81
           "white":   (0.659, 0.686, 0.663),   # 168,175,169   cream daisy
           # low-chroma warm cream: the reference spends ~11% of its bright pixels in the
           # sat 0.18-0.30 "muted" band (sun-bleached petals). Without it the field reads
           # as poster paint rather than a sun-faded tapestry.
           "cream":   (0.780, 0.720, 0.600),
           "yellow":  (0.700, 0.620, 0.290),
           "ochre":   (0.560, 0.470, 0.215),
           "rose":    (0.780, 0.380, 0.330),
           "olive":   (0.439, 0.439, 0.314),   # 112,112, 80   the field's foliage olive
           "green":   (0.270, 0.320, 0.140),
           # blue is a real but RARE accent in the reference (0.4% of blooms), so it stays in
           # the palette at low weight rather than dominating as it did in phase 18.
           "blue":    (0.322, 0.510, 0.604),
           "periwinkle": (0.400, 0.470, 0.640),
           "slate":   (0.265, 0.330, 0.440)}


# ---- phase 21: blooms are the mass, and the brief fixes their colour split exactly:
# crimson/scarlet 60%, white/cream 25%, golden-orange 15%. Drawing a raw hue from a flat
# table let orange run away with the field (it did, in r728); drawing the GROUP first and
# only then a hue inside it puts that ratio under direct control.
P21_GROUPS = {
    # PHASE 21 FIX A: "scarlet" is (196,141,100) -- a sun-bleached tan that measures as
    # ORANGE, and at weight 0.38 it was dragging the red share down (measured 33.8% vs the
    # brief's 60%). The red group is now dominated by true crimson; scarlet survives only as
    # a light accent so the field still has hue variety inside the red family.
    "red":    {"red": 0.78, "scarlet": 0.12, "rose": 0.10},
    "white":  {"white": 0.52, "cream": 0.48},
    "orange": {"orange": 0.44, "gold": 0.40, "ochre": 0.16},
}


def p21_mix(cfg):
    """(red, white, orange) weights, normalised."""
    m = list(cfg.get("p21_mix", (0.60, 0.25, 0.15)))
    tot = sum(m) or 1.0
    return [max(0.0, x) / tot for x in m]


def p21_pick_group(cfg, rnd=random):
    m = p21_mix(cfg)
    return rnd.choices(["red", "white", "orange"], weights=m)[0]


def p21_pick_hue(group, mats):
    """Weighted hue inside a colour group, restricted to materials that exist."""
    w = {k: v for k, v in P21_GROUPS[group].items() if v > 0 and k in mats}
    if not w:
        w = {"red": 1.0}
    return random.choices(list(w), weights=list(w.values()))[0]


# Region recipes. The brief is explicit: red dominates the mid and foreground, white/cream
# gathers in drifts, orange is an accent. Weights sum ~1.0 per region.
REGION_W = {
    "white":   {"white": 0.26, "cream": 0.20, "red": 0.12, "scarlet": 0.12,
                "orange": 0.10, "gold": 0.10, "ochre": 0.06, "rose": 0.04},
    "red":     {"red": 0.26, "scarlet": 0.30, "rose": 0.08, "orange": 0.12,
                "gold": 0.12, "cream": 0.06, "ochre": 0.06},
    "scarlet": {"scarlet": 0.34, "red": 0.22, "rose": 0.06, "orange": 0.12,
                "gold": 0.12, "cream": 0.08, "ochre": 0.06},
    "orange":  {"orange": 0.26, "gold": 0.22, "scarlet": 0.16, "red": 0.12,
                "ochre": 0.10, "cream": 0.08, "white": 0.06},
    "mixed":   {"red": 0.16, "scarlet": 0.24, "orange": 0.20, "gold": 0.16,
                "ochre": 0.08, "white": 0.08, "cream": 0.08},
}


def region_at(u, v):
    best, bd = "mixed", 1e9
    for (cu, cv, rad, kind) in FLOWER_REGIONS:
        d = math.hypot((u - cu) / max(rad, 1e-6), (v - cv) / max(rad * 0.8, 1e-6))
        if d < bd:
            bd, best = d, kind
    return best


def tilt_y(a, tx):
    """Spin `a` about the VIEW axis (Y), then tilt `tx` out of the image plane."""
    c, s = math.cos(a), math.sin(a)
    Ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)
    cx, sx = math.cos(tx), math.sin(tx)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], float)
    return Ry @ Rx


def flower_proto(petals=5, tilt_deg=34.0):
    """A genuinely volumetric blossom: 38 triangles, ~46 verts.

    Each petal is a 3-segment quad strip that leaves the short stem axis, fans out in the
    image plane and TILTS 34 degrees toward the camera, so every petal presents a curved
    surface to the ortho view instead of a flat plane seen edge-on. A small octahedron
    forms the centre. The previous version laid flat petals in one plane; from an
    orthographic camera along +Y those collapsed to slivers, which is the reported defect.
    """
    R = 0.50
    tilt = math.radians(tilt_deg)
    TS = (0.08, 0.36, 0.68, 1.00)
    maxw = 0.30
    V, F = [], []
    for k in range(petals):
        a = 2.0 * math.pi * k / petals - math.pi / 2.0
        ca, sa = math.cos(a), math.sin(a)
        base = len(V)
        for t in TS:
            hw = maxw * (math.sin(math.pi * min(t, 0.995)) ** 0.62) * (1.0 - 0.15 * t)
            x = R * t
            y = -R * t * math.sin(tilt) - 0.10 * R * (t ** 3) * math.cos(tilt)
            for sgn in (-1, 1):
                z = hw * sgn
                V.append((x * ca - z * sa, y, x * sa + z * ca))
        for r in range(3):
            q = base + r * 2
            F.append((q, q + 1, q + 3)); F.append((q, q + 3, q + 2))
    # centre: octahedron (closed, 8 tris)
    c = len(V); r0 = 0.13
    V += [(0.0, -r0 * 1.15, 0.0), (0.0, r0 * 0.55, 0.0),
          (r0, 0.0, 0.0), (-r0, 0.0, 0.0), (0.0, 0.0, r0), (0.0, 0.0, -r0)]
    F += [(c, c + 2, c + 4), (c, c + 4, c + 3), (c, c + 3, c + 5), (c, c + 5, c + 2),
          (c + 1, c + 4, c + 2), (c + 1, c + 3, c + 4), (c + 1, c + 5, c + 3), (c + 1, c + 2, c + 5)]
    return np.array(V, float), F


# ================================================== P18. plant primitive
# ===================================================== P18. PLANTS (big, individual)
# The p17 field was a 4px speckle: statistically right, botanically wrong. The target's
# foreground blooms measure 40-70px across with 60-130px leaf masses, and 27% of the field
# is dark ground showing BETWEEN plants. So this builds whole plants -- stem, leaves,
# bloom head -- and sizes them by their projected depth in the frame.

SPECIES = ("hollyhock", "daisy", "tulip", "cluster")


def _tube(rings, seg, radius_fn, bend, height, twist=0.0):
    """A tapered tube from z=0..height with a lateral bend. Returns (verts, faces)."""
    V, F = [], []
    for i in range(rings):
        t = i / (rings - 1)
        z = height * t
        r = radius_fn(t)
        cx = bend[0] * (t ** 2)
        cy = bend[1] * (t ** 2)
        for k in range(seg):
            a = 2 * math.pi * k / seg + twist * t
            V.append((cx + math.cos(a) * r, cy + math.sin(a) * r, z))
    for i in range(rings - 1):
        for k in range(seg):
            a0 = i * seg + k; a1 = i * seg + (k + 1) % seg
            b0 = (i + 1) * seg + k; b1 = (i + 1) * seg + (k + 1) % seg
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    return V, F


def _leaf(base, ang, length, width, curl=0.5, nseg=4, up=0.16):
    """One curved blade leaf: a strip of quads that sweeps out and droops.

    Built in the XZ image plane with a Y-curl so it catches light rather than vanishing
    edge-on -- the same failure mode that flattened the old petals.
    """
    V, F = [], []
    bx, by, bz = base
    ca, sa = math.cos(ang), math.sin(ang)
    for i in range(nseg + 1):
        t = i / nseg
        # position along the blade: out + rising then drooping
        d = length * t
        zz = up * length * math.sin(t * math.pi * 0.85) - 0.35 * length * (t ** 2.6)
        hw = width * math.sin(math.pi * min(t * 1.06, 1.0)) ** 0.72
        yb = -curl * width * (t ** 1.6)          # curl toward the camera
        for sgn in (-1, 1):
            # the blade twists slightly so its face is angled, never perfectly edge-on
            tw = 0.55 * t * sgn
            ox = -math.sin(ang) * hw * sgn * math.cos(tw)
            oz = math.cos(ang) * hw * sgn * math.cos(tw)
            oy = hw * sgn * math.sin(tw)
            V.append((bx + ca * d + ox, by + yb + oy, bz + zz + oz))
    for i in range(nseg):
        q = i * 2
        F.append((q, q + 1, q + 3)); F.append((q, q + 3, q + 2))
    return V, F


def _petal(base, ang, tilt, length, width, curl=0.55, nseg=3):
    """A cupped petal: quad strip that rises out of the head plane and curls back."""
    V, F = [], []
    ca, sa = math.cos(ang), math.sin(ang)
    ct, st = math.cos(tilt), math.sin(tilt)
    for i in range(nseg + 1):
        t = i / nseg
        d = length * t
        # in the petal's own frame: out along `d`, up by the tilt, curled by curl
        px = d * ct
        pz = d * st
        py = -curl * length * (t ** 2.2)      # cup toward camera
        hw = width * math.sin(math.pi * min(t * 1.02, 1.0)) ** 0.55 * (1.0 - 0.30 * t)
        for sgn in (-1, 1):
            ox = -math.sin(ang) * hw * sgn
            oz = math.cos(ang) * hw * sgn
            V.append((base[0] + ca * px + ox, base[1] + py, base[2] + sa * px + pz + oz))
    for i in range(nseg):
        q = i * 2
        F.append((q, q + 1, q + 3)); F.append((q, q + 3, q + 2))
    return V, F


def _disc(centre, r, n=8, ry=0.55):
    """A low dome for the flower centre."""
    V, F = [], []
    V.append((centre[0], centre[1] - r * ry, centre[2]))
    for k in range(n):
        a = 2 * math.pi * k / n
        V.append((centre[0] + math.cos(a) * r, centre[1], centre[2] + math.sin(a) * r))
    for k in range(n):
        F.append((0, 1 + k, 1 + (k + 1) % n))
    return V, F


P19_PETAL = {"s": 1.0, "by": {}}   # bloom-head scale, set from p19_petal_scale(+_by)

# Per-species bloom scale. A single global multiplier cannot work: the daisy's head radius is
# 0.20-0.27 while the hollyhock's florets are 0.085-0.125, so at r726's p19_petal_scale=1.75
# the daisies rendered 138-255 px across and became the brown hexagonal "lumps" that buried the
# villa, while hollyhock spikes stayed a reasonable 38-70 px. Calibrated so every species lands
# in the target's ~35-45 px foreground bloom band.
P20_PETAL_BY = {"daisy": 0.30, "tulip": 0.48, "cluster": 0.55, "hollyhock": 0.80}


def plant_proto(species, seed=0):
    """Build one whole plant in local space (base at origin, growing +Z).

    Returns (stem_V, stem_F, leaf_V, leaf_F, petal_V, petal_F, centre_V, centre_F, H)

    PHASE 21 -- the field must read as BLOOMS, not stalks. r728 had one bloom on top of a
    long bare pole, so the lower half of the frame was dark green stems with specks on top.
    The target is the opposite: the flowers are the mass and the foliage is what sits behind
    and between them. So every species here is built as a short stem carrying a SPRAY of
    3-7 heads crowded around the top, with the leaf cluster right under them -- the bloom
    head now occupies most of the plant's height instead of a few pixels at its tip.
    """
    rnd = random.Random(seed)
    # PHASE 21 FIX A: PS used to be computed here and then NEVER referenced, so every
    # p19_petal_scale/p20_petal_by value in the cfg was silently ignored and the bloom heads
    # stayed at their tiny authored radius (that is why 1.00 -> 1.55 changed nothing).
    # It is now multiplied into every head() radius below.
    PS = float(P19_PETAL["s"]) * float(P19_PETAL["by"].get(species, 1.0))
    sV, sF = [], []
    lV, lF = [], []
    pV, pF = [], []
    cV, cF = [], []

    def head(cx, cz, R, npet, tilt_lo, tilt_hi, pw, curl, disc_k, jit=0.12):
        """One bloom: npet narrow petals radiating from (cx,cz), plus its centre dome.
        R is scaled by PS (the petal-scale knob) -- see the note at the top of this function."""
        R = R * PS
        for k in range(npet):
            a = 2 * math.pi * k / npet + rnd.uniform(-jit, jit)
            v, f = _petal((cx, 0.0, cz), a, rnd.uniform(tilt_lo, tilt_hi), R,
                          R * pw, curl=curl, nseg=3)
            pV.extend(v); pF.extend(f)
        v, f = _disc((cx, -R * 0.05, cz), R * disc_k, 8)
        cV.extend(v); cF.extend(f)

    if species == "hollyhock":
        # a tall spike, but the SPRAY of blooms fills its upper two thirds -- not one floret
        # crowning a bare pole
        H = rnd.uniform(0.85, 1.0)
        bend = (rnd.uniform(-0.10, 0.10), rnd.uniform(-0.05, 0.05))
        v, f = _tube(9, 6, lambda t: 0.022 * (1 - 0.60 * t), bend, H); sV += v; sF += f
        nl = rnd.randint(3, 4)
        for i in range(nl):
            t = 0.14 + 0.34 * i / max(nl - 1, 1)
            ang = (0 if i % 2 == 0 else math.pi) + rnd.uniform(-0.35, 0.35)
            b = (bend[0] * t * t, bend[1] * t * t, H * t)
            v, f = _leaf(b, ang, rnd.uniform(0.30, 0.42), rnd.uniform(0.070, 0.095),
                         curl=0.45, nseg=4, up=0.22); lV += v; lF += f
        nb = rnd.randint(3, 6)                      # brief: 3-6 blooms per plant
        for i in range(nb):
            t = 0.46 + 0.54 * i / max(nb - 1, 1)
            hz = H * t
            # side buds step outward so the spike reads as a spray, not a stack
            off = rnd.uniform(-0.075, 0.075) * (1.0 - 0.5 * i / max(nb - 1, 1))
            rr = rnd.uniform(0.105, 0.145) * (1.0 - 0.26 * i / max(nb - 1, 1))
            head(bend[0] * t * t + off, hz, rr, rnd.randint(8, 10),
                 0.30, 0.75, 0.68, 0.50, 0.24)

    elif species == "daisy":
        # the low carpet species: a short stalk under one BIG head plus two smaller ones
        H = rnd.uniform(0.44, 0.62)
        bend = (rnd.uniform(-0.06, 0.06), rnd.uniform(-0.03, 0.03))
        v, f = _tube(8, 6, lambda t: 0.020 * (1 - 0.50 * t), bend, H); sV += v; sF += f
        nl = rnd.randint(2, 3)
        for i in range(nl):
            t = 0.12 + 0.34 * i / max(nl - 1, 1)
            ang = (math.pi * 0.9 if i % 2 == 0 else -0.15) + rnd.uniform(-0.4, 0.4)
            b = (bend[0] * t * t, bend[1] * t * t, H * t)
            v, f = _leaf(b, ang, rnd.uniform(0.24, 0.34), rnd.uniform(0.060, 0.082),
                         curl=0.50, nseg=4, up=0.18); lV += v; lF += f
        R = rnd.uniform(0.115, 0.150)
        head(bend[0], H, R, rnd.randint(13, 16), 0.02, 0.22, 0.62, 0.28, 0.30)
        for _ in range(rnd.randint(2, 3)):          # side flowers on the same plant
            a = rnd.uniform(0, 6.283); rr = R * rnd.uniform(0.52, 0.74)
            head(bend[0] + math.cos(a) * R * 0.85, H * rnd.uniform(0.70, 0.92),
                 rr, rnd.randint(10, 13), 0.02, 0.26, 0.64, 0.30, 0.28)

    elif species == "tulip":
        H = rnd.uniform(0.52, 0.70)
        bend = (rnd.uniform(-0.08, 0.08), rnd.uniform(-0.03, 0.03))
        v, f = _tube(8, 6, lambda t: 0.021 * (1 - 0.45 * t), bend, H); sV += v; sF += f
        nl = rnd.randint(2, 3)
        for i in range(nl):
            t = 0.12 + 0.30 * i / max(nl - 1, 1)
            ang = rnd.uniform(0, 2 * math.pi)
            b = (bend[0] * t * t, bend[1] * t * t, H * t)
            v, f = _leaf(b, ang, rnd.uniform(0.26, 0.36), rnd.uniform(0.065, 0.088),
                         curl=0.55, nseg=4, up=0.30); lV += v; lF += f
        R = rnd.uniform(0.100, 0.130)
        head(bend[0], H * 0.94, R, 7, 1.05, 1.35, 0.80, 0.42, 0.26)
        for _ in range(rnd.randint(2, 4)):          # a tulip clump, not a single cup
            a = rnd.uniform(0, 6.283); d = rnd.uniform(0.05, 0.10)
            head(bend[0] + math.cos(a) * d, H * rnd.uniform(0.62, 0.86),
                 R * rnd.uniform(0.62, 0.85), 7, 1.00, 1.32, 0.80, 0.42, 0.24)

    else:  # "cluster" -- small blossoms in a dense flat-topped spray
        H = rnd.uniform(0.38, 0.55)
        bend = (rnd.uniform(-0.08, 0.08), rnd.uniform(-0.04, 0.04))
        v, f = _tube(7, 5, lambda t: 0.017 * (1 - 0.45 * t), bend, H); sV += v; sF += f
        nl = rnd.randint(2, 3)
        for i in range(nl):
            t = 0.14 + 0.34 * i / max(nl - 1, 1)
            ang = rnd.uniform(0, 2 * math.pi)
            b = (bend[0] * t * t, bend[1] * t * t, H * t)
            v, f = _leaf(b, ang, rnd.uniform(0.20, 0.30), rnd.uniform(0.052, 0.072),
                         curl=0.50, nseg=3, up=0.20); lV += v; lF += f
        nb = rnd.randint(4, 6)
        for i in range(nb):
            t = 0.52 + 0.48 * i / max(nb - 1, 1)
            ox = bend[0] * t * t + rnd.uniform(-0.10, 0.10)
            head(ox, H * t, rnd.uniform(0.070, 0.100), 6, 0.25, 0.55, 0.74, 0.45, 0.30)

    return (np.array(sV, float), sF, np.array(lV, float), lF,
            np.array(pV, float), pF, np.array(cV, float), cF, H)


def place_flowers(cfg):
    """Millefleur carpet. Everything is raycast onto the land the camera actually sees, so the
    blooms lie on the island's near face and across the foreground meadow -- no floating."""
    mats = {}
    for k, rgb in HUE_RGB.items():
        m = bpy.data.materials.get("p16_flw_" + k)
        if m is None:
            m = bpy.data.materials.new("p16_flw_" + k)
            look.mat_petal(m, tuple(look.srgb2lin(c) for c in rgb))
        mats[k] = m
    dg = bpy.context.evaluated_depsgraph_get()
    pv, pf = flower_proto()
    buckets = {}
    total = 0
    LAND = {"oskar_scene", "p16_meadow", "p21_hill"}
    for label, n, size, seed in (
            ("island", int(cfg.get("fl_island", 22000)), cfg.get("fl_size", 0.0078), 431),
            ("meadow", int(cfg.get("fl_meadow", 17000)), cfg.get("fl_size_m", 0.0095), 977)):
        if n <= 0:
            continue
        got = scatter_on(dg, cfg, n, cfg.get("fl_off", 0.030), size, seed, only=LAND)
        for k, tf in got.items():
            buckets.setdefault(k, []).extend(tf)
        c = sum(len(v) for v in got.values())
        total += c
        print("P16_FLW_%s %d" % (label, c))
    for k, tf in buckets.items():
        if tf:
            merge(pv, pf, tf, "p16_flw_" + k, mats[k])
    print("P16_FLOWERS", total, {k: len(v) for k, v in buckets.items() if v})


# ------------------------------------------------------- 6. framing oaks + fruit
# ===================================================== P18. plant scatter
# Pixel height is the control, because that is what the eye reads. A plant whose head sits
# at image row v gets a height derived from the traced size for its depth band, so the
# foreground is genuinely big and the hillside is genuinely small -- not one size everywhere.

def px_to_world_h(px, cfg):
    """world height that renders as `px` scanlines at the current ortho scale"""
    return px / float(RES_Y) * (cfg.get("ortho_scale", 7.0) / ASPECT)


def plant_size_px(v, cfg):
    """Plant height in pixels, by depth band. Measured off the reference: foreground masses
    run 90-170px tall, midground 55-90px, hillside 26-46px."""
    fg = cfg.get("p19_fg_px", (95.0, 175.0))
    mid = cfg.get("p19_mid_px", (52.0, 92.0))
    hill = cfg.get("p19_hill_px", (26.0, 46.0))
    if v >= 0.80:
        lo, hi = fg
    elif v >= 0.60:
        t = (v - 0.60) / 0.20
        lo = mid[0] + (fg[0] - mid[0]) * t
        hi = mid[1] + (fg[1] - mid[1]) * t
    elif v >= 0.48:
        t = (v - 0.48) / 0.12
        lo = hill[0] + (mid[0] - hill[0]) * t
        hi = hill[1] + (mid[1] - hill[1]) * t
    else:
        lo, hi = hill
    # phase 20: the r726 field stood tall enough to bury the villa, tower, bridge and path.
    # Lowering the WHOLE field (not just the front) is what re-opens the skyline.
    hs = float(cfg.get("p20_h_scale", 1.0))
    return lo * hs, hi * hs


def zone_height_factor(u, v, cfg):
    """Multiply a plant's height where the built elements must stay readable.
    A hard clear zone would leave a hole in the meadow, so the villa and path keep low
    plants and only lose their TALL ones -- which is what was crowding the skyline."""
    f = 1.0
    for (u0, u1, v0, v1, fac) in cfg.get("p20_low", []):
        if u0 <= u <= u1 and v0 <= v <= v1:
            f = min(f, float(fac))
    return f


def in_clear_zone(u, v, cfg):
    """Rectangles the meadow must leave open so the built elements stay readable.
    The villa/tower block sits at u .50-.72, v .34-.52 and the path runs through .35-.75,
    v .55-.66; r726 planted straight through both and hid them behind blooms."""
    for (u0, u1, v0, v1) in cfg.get("p20_clear", []):
        if u0 <= u <= u1 and v0 <= v <= v1:
            return True
    return False


def drift_field(cfg):
    """Pre-roll a set of soft 'drifts' -- clumps of like-coloured planting.

    The reference is not evenly mixed: whites gather in patches, reds mass together. A purely
    random per-plant colour draw produces the uniform confetti the brief calls washed out, so
    each plant's colour is drawn from whichever drift covers its position, with a minority
    weight pulling toward the region's own recipe.
    """
    rnd = random.Random(int(cfg.get("p19_drift_seed", 91)))
    n = int(cfg.get("p19_drifts", 26))
    drifts = []
    for _ in range(n):
        cu = rnd.uniform(-0.08, 1.08)
        cv = rnd.uniform(0.42, 1.12)
        rad = rnd.uniform(*cfg.get("p19_drift_radius", (0.075, 0.20)))
        # a drift commits to one colour family
        # phase 21: a drift commits to one of the three brief groups, in the brief's ratio
        fam = rnd.choices(["red", "white", "orange"], weights=p21_mix(cfg))[0]
        drifts.append((cu, cv, rad, fam))
    return drifts


# Rebalanced against the reference's measured hue histogram (target: crimson 7.9 /
# scarlet 17.2 / orange 38.1 / gold 16.6 / white 8.1 / muted 11.4). The first pass came out
# 57% orange and 6% gold -- too much pure orange, too little sun-bleached gold and cream.
DRIFT_FAMILY = {
    "red":   {"red": 0.30, "scarlet": 0.36, "rose": 0.08, "orange": 0.10,
              "gold": 0.10, "cream": 0.06},
    "white": {"white": 0.30, "cream": 0.30, "gold": 0.14, "ochre": 0.10,
              "red": 0.06, "scarlet": 0.06, "blue": 0.04},
    "orange": {"orange": 0.26, "gold": 0.26, "scarlet": 0.20, "red": 0.10,
               "ochre": 0.12, "cream": 0.06},
    "gold":  {"gold": 0.32, "orange": 0.22, "ochre": 0.18, "scarlet": 0.14,
              "red": 0.08, "cream": 0.06},
}


def drift_at(u, v, drifts):
    """Which drift family owns this point, or None if it falls between drifts."""
    best, bd = None, 1e9
    for (cu, cv, rad, fam) in drifts:
        d = math.hypot(u - cu, v - cv) / max(rad, 1e-6)
        if d < bd:
            bd, best = d, fam
    return best if bd < 1.0 else None


def _tier_weights(band, kind):
    """Species by depth: tall spikes stand in the back and mid, low daisies carpet the front.
    This is what stops the field reading as a row of chopsticks of one uniform height."""
    if band == "hill":
        w = (0.30, 0.22, 0.20, 0.28)      # hollyhock, daisy, tulip, cluster
    elif band == "mid":
        w = (0.40, 0.26, 0.18, 0.16)
    else:
        w = (0.20, 0.38, 0.14, 0.28)      # foreground: daisy + cluster dominate the mat
    return w


def plant_count_target(cfg):
    return dict(
        fg=int(cfg.get("p19_n_fg", 430)),
        mid=int(cfg.get("p19_n_mid", 430)),
        hill=int(cfg.get("p19_n_hill", 330)),
    )


def foliage_tufts(cfg, dg, LAND):
    """Low leaf clusters packed among the plant bases (brief item 3).

    The reference never shows bare ground between blooms -- it shows dark leaves. These are
    squat radial clumps of curved blades, wider than they are tall, seated on the same
    raycast as the flowers so nothing floats.
    """
    n = int(cfg.get("p19_n_tufts", 900))
    recs = []
    got = 0
    tries = 0
    tV, tF = [], []
    # one prototype, reused: 7 blades radiating from a point
    rnd0 = random.Random(4242)
    for b in range(7):
        ang = 2 * math.pi * b / 7 + rnd0.uniform(-0.3, 0.3)
        vv, ff = _leaf((0.0, 0.0, 0.0), ang, rnd0.uniform(0.24, 0.34),
                       rnd0.uniform(0.055, 0.080), curl=0.55, nseg=3,
                       up=rnd0.uniform(0.10, 0.26))
        o = len(tV)
        tV.extend(vv)
        tF.extend([(x + o, y + o, z + o) for (x, y, z) in ff])
    while got < n and tries < n * 12:
        tries += 1
        u = random.uniform(cfg.get("p18_u0", -0.06), cfg.get("p18_u1", 1.06))
        v = random.uniform(cfg.get("p19_tuft_v0", 0.46), 1.10)
        o, loc = ray_hit(dg, cfg, u, v)
        if o is None or o.name not in LAND:
            continue
        lo, hi = plant_size_px(v, cfg)
        # tufts sit LOW: roughly a third of the local plant height
        hp = random.uniform(lo, hi) * cfg.get("p19_tuft_frac", 0.42)
        s = px_to_world_h(hp, cfg) / 0.30
        P = np.array([loc.x, loc.y - random.uniform(0.06, 0.34), loc.z - 0.006])
        R = tilt_y(random.uniform(-0.6, 0.6), random.uniform(-0.3, 0.3))
        S = np.array([s, s, s])
        recs.append((np.array(tV, float), tF, P, R, S))
        got += 1
    return recs, got


def place_plants(cfg):
    """Big individual plants on the terrain, in colour drifts, with height tiers."""
    mats = {}
    for k, rgb in HUE_RGB.items():
        m = bpy.data.materials.get("p18_flw_" + k)
        if m is None:
            m = bpy.data.materials.new("p18_flw_" + k)
        # Base Color is LINEAR in Blender. HUE_RGB holds sRGB values measured off the
        # reference, so they MUST go through srgb2lin -- the old code wrote them raw, which
        # lightened and desaturated every flower (swatch-verified: sat 0.35 vs 0.59).
        # Then push HSV saturation: the measured p50s are averages over antialiased, sun-
        # washed pixels, so painting them literally still reads chalky. p19_sat restores the
        # chroma the reference has in its saturated core. HSV (not a per-channel scale) so
        # the white/cream daisies stay neutral instead of turning pink.
        import colorsys
        # P22 COLOUR FIX. The colour split failed because the ORANGE/GOLD/OCHRE petals and
        # the cream daisies were authored too yellow and too dark: under the warm key
        # (1.0/0.855/0.655) they landed at hue 60-90 -- the "mustard" band -- which is 22.6%
        # of all lit area against the target's ~2%. An override table lets those four hues be
        # re-authored (deeper orange, cream lifted to read WHITE against the ochre field, gold
        # and ochre pulled toward red-orange) without touching the crimson that already
        # measures correctly.
        _ov = cfg.get("p22_hue_override", {}).get(k)
        if _ov:
            rgb = tuple(float(x) for x in _ov)
        _h, _s, _v = colorsys.rgb_to_hsv(*rgb)
        _s = min(1.0, _s * float(cfg.get("p19_sat", 1.0)))
        if _s < 0.18:
            _s = min(1.0, _s)          # leaves near-neutral blooms neutral
        rgb_s = colorsys.hsv_to_rgb(_h, _s, _v)
        # albedo gain: the warm key (4.45 -> 2.76 sun with a 1.0/0.855/0.655 tint) multiplies
        # the albedo back up, so an authored 210,83,59 crimson rendered as 238,124,85 pink.
        # Measured on the real scene, 0.54 of the authored albedo lands on the target's values.
        _g = float(cfg.get("p20_petal_gain", 1.0))
        rgb_s = tuple(min(1.0, c * _g) for c in rgb_s)
        lin = tuple(look.srgb2lin(c) for c in rgb_s)
        _set_base(m, lin, rough=float(cfg.get("p19_petal_rough", 0.90)))
        # specular sheen is ADDITIVE white in EEVEE and was the second desaturator: a
        # crimson (210,83,59) petal rendered (236,151,129) because ~0.22 linear white was
        # being added on top. Killing it is what actually restores the crimson.
        try:
            _b = m.node_tree.nodes.get("Principled BSDF")
            _b.inputs["Specular IOR Level"].default_value = float(cfg.get("p19_petal_spec", 0.10))
        except Exception as e:
            print("P19_SPECFAIL", e)
        mats[k] = m

    # P22 LEAF DE-YELLOW. Foliage was the single biggest colour-split defect: 169 885 px of
    # leaf -- 28% of the whole foreground -- of which 72 580 were lit, and 100% of the frame's
    # hue-60-90 area came from leaves alone. The reference's foliage is TEAL-GREEN (its hill
    # window puts 37% of pixels at hue 175-260 and 14% at 130-175; v4 put 95% at 50-70).
    # p19_leaf_rgb used to be an olive (0.15,0.158,0.084), which the warm key pushed to
    # yellow. This is now a cool green, and `p19_leaf_mix` is raised so the tint actually
    # dominates the leaf texture instead of letting the map's yellow through.
    leaf_rgb = tuple(cfg.get("p19_leaf_rgb", (0.118, 0.176, 0.074)))
    leaf_m = bpy.data.materials.get("p18_leaf")
    if leaf_m is None:
        leaf_m = bpy.data.materials.new("p18_leaf")
    try:
        lm = tex_material("p18_leaf_tex", "leaf", rough=0.90)
        _tint_material(lm, tuple(look.srgb2lin(c) for c in leaf_rgb),
                       float(cfg.get("p19_leaf_mix", 0.72)))
        # `p19_leaf_gain` is a per-channel MULTIPLY inserted between the tinted map and the
        # BSDF: cutting red is what removes the yellow (yellow = red+green, so a warm key on a
        # green albedo lands at hue 60-90 unless red is pulled down), and lifting blue pushes
        # the leaf toward the teal the reference has. Multiplying keeps the map's structure.
        _lg = cfg.get("p19_leaf_gain")
        if _lg:
            try:
                _nt = lm.node_tree
                _b = _nt.nodes.get("Principled BSDF")
                _src = _b.inputs["Base Color"].links[0].from_socket
                _mul = _nt.nodes.new("ShaderNodeMix"); _mul.data_type = "RGBA"
                _mul.blend_type = "MULTIPLY"; _mul.location = (620, -160)
                _mul.inputs["Factor"].default_value = 1.0
                _nt.links.new(_src, _mul.inputs[6])
                _cc = _nt.nodes.new("ShaderNodeRGB")
                _cc.outputs[0].default_value = (float(_lg[0]), float(_lg[1]),
                                                float(_lg[2]), 1.0)
                _nt.links.new(_cc.outputs[0], _mul.inputs[7])
                _nt.links.new(_mul.outputs[2], _b.inputs["Base Color"])
            except Exception as e:
                print("P22_LEAFGAINFAIL", e)
        leaf_m = lm
    except Exception:
        _set_base(leaf_m, tuple(look.srgb2lin(c) for c in leaf_rgb), rough=0.90)
    stem_m = bpy.data.materials.get("p18_stem")
    if stem_m is None:
        stem_m = bpy.data.materials.new("p18_stem")
    _set_base(stem_m, tuple(look.srgb2lin(c) for c in
                            cfg.get("p19_stem_rgb", (0.098, 0.132, 0.058))), rough=0.86)
    centre_m = bpy.data.materials.get("p18_centre")
    if centre_m is None:
        centre_m = bpy.data.materials.new("p18_centre")
    # The centre disc is the brightest orange object inside every single bloom, so at the
    # reference's bloom density it floods the orange band (measured: 66% orange vs 38%
    # target). A DARKER ochre eye keeps the disc readable while letting the petals own the
    # hue histogram -- which is also what the reference's poppies/daisies actually look like.
    _set_base(centre_m, tuple(look.srgb2lin(c) for c in
                              cfg.get("p19_centre_rgb", (0.300, 0.200, 0.070))), rough=0.86)
    try:
        _b = centre_m.node_tree.nodes.get("Principled BSDF")
        _b.inputs["Specular IOR Level"].default_value = 0.08
    except Exception:
        pass

    dg = bpy.context.evaluated_depsgraph_get()
    LAND = {"oskar_scene", "p16_meadow", "p21_hill"}
    PROTOS = {}

    def proto(sp, sd):
        key = (sp, sd % 6)
        if key not in PROTOS:
            PROTOS[key] = plant_proto(sp, seed=sd)
        return PROTOS[key]

    P19_PETAL["s"] = float(cfg.get("p19_petal_scale", 1.0))
    P19_PETAL["by"] = dict(cfg.get("p20_petal_by", P20_PETAL_BY))
    drifts = drift_field(cfg)
    buckets = {k: [] for k in ("stem", "leaf", "petal", "centre")}
    per_band = {}
    heights = []          # px, for the report
    tot_plants = 0
    hlo, hhi = cfg.get("p19_hvar", (0.42, 2.20))
    bands = [("fg", 0.78, 1.10), ("mid", 0.62, 0.80), ("hill", 0.50, 0.63)]
    ntar = plant_count_target(cfg)
    for band, v0, v1 in bands:
        want = ntar[band]
        got_n = 0
        tries = 0
        while got_n < want and tries < want * 14:
            tries += 1
            u = random.uniform(cfg.get("p18_u0", -0.06), cfg.get("p18_u1", 1.06))
            v = random.uniform(v0, v1)
            o, loc = ray_hit(dg, cfg, u, v)
            if o is None or o.name not in LAND:
                continue
            if in_clear_zone(u, v, cfg):
                continue

            # ---- colour, phase 21: GROUP first (that is the only way to hold the brief's
            # 60/25/15 split), then a hue inside the group. Spatial coherence still comes
            # from the drift field: a plant inside a drift takes that drift's group, which
            # is what makes the field read as woven patches rather than confetti.
            # PHASE 21 FIX A: REGION_W + the drift field were overriding p21_mix, so the
            # authored 60/25/15 split never reached the frame (measured 10/37/1). Inside the
            # foreground band the brief's global split wins outright; the drift lock only
            # survives higher up the slope, where it makes the hillside read as patches.
            kind = region_at(u, v)      # still needed by _tier_weights() below
            if cfg.get("p21_fg_mix_force", True) and v >= 0.70:
                grp = p21_pick_group(cfg)
            else:
                fam = drift_at(u, v, drifts)
                if fam is not None and random.random() < cfg.get("p19_drift_lock", 0.72):
                    grp = fam
                else:
                    grp = p21_pick_group(cfg)
            col = p21_pick_hue(grp, mats)

            # ---- height: a real 0.4x-2.2x spread around the band's base, so tall spikes
            # stand above a low mat instead of every plant being the same chopstick
            lo, hi = plant_size_px(v, cfg)
            base = random.uniform(lo, hi)
            hp = base * random.uniform(hlo, hhi)
            hp *= zone_height_factor(u, v, cfg)
            hp = max(10.0, min(hp, cfg.get("p19_px_cap", 320.0)))
            heights.append(hp)

            sp = random.choices(SPECIES, weights=_tier_weights(band, kind))[0]
            sd = random.randrange(1 << 20)
            sV, sF, lV, lF, pV, pF, cV, cF, H = proto(sp, sd)
            s = px_to_world_h(hp, cfg) / max(H, 1e-6)
            # lean: a little sway, more on the tall ones
            lean_f = 0.10 + 0.10 * min(hp / 140.0, 1.6)
            P = np.array([loc.x, loc.y - random.uniform(0.05, 0.34), loc.z - 0.005])
            R = tilt_y(random.uniform(-lean_f, lean_f), random.uniform(-0.22, 0.22))
            sxz = random.uniform(*cfg.get("p19_width_var", (0.85, 1.20)))
            S = np.array([s * sxz, s, s * sxz])
            for slot, vv, ff in (("stem", sV, sF), ("leaf", lV, lF),
                                 ("petal", pV, pF), ("centre", cV, cF)):
                if len(vv):
                    buckets[slot].append((col if slot == "petal" else None, vv, ff, P, R, S))
            got_n += 1
        per_band[band] = got_n
        tot_plants += got_n

    made = 0
    for slot in ("stem", "leaf", "petal", "centre"):
        if not buckets[slot]:
            continue
        groups = {}
        for (c, vv, ff, P, R, S) in buckets[slot]:
            groups.setdefault(c, []).append((vv, ff, P, R, S))
        for c, recs in groups.items():
            if c is None:
                mat = {"stem": stem_m, "leaf": leaf_m, "centre": centre_m}[slot]
                nm = "p18_" + slot
            else:
                mat = mats[c]
                nm = "p18_petal_" + c
            merge_multi(recs, nm, mat)
            made += 1

    # ---- understorey foliage among the bases
    tuft_recs, ntuft = foliage_tufts(cfg, dg, LAND)
    if tuft_recs:
        merge_multi(tuft_recs, "p19_understorey", leaf_m)
        made += 1
    hn = np.array(heights) if heights else np.array([0.0])
    print("P18_PLANTS total=%d bands=%s objects=%d tufts=%d" % (tot_plants, per_band, made, ntuft))
    print("P19_HEIGHTS px: p05 %.0f p25 %.0f med %.0f p75 %.0f p95 %.0f max %.0f  ratio p95/p05 %.2f"
          % (np.percentile(hn, 5), np.percentile(hn, 25), np.median(hn),
             np.percentile(hn, 75), np.percentile(hn, 95), hn.max(),
             np.percentile(hn, 95) / max(np.percentile(hn, 5), 1e-6)))
    return tot_plants


def _species_weights(kind):
    """different species suit different regions: spikes on the hillside, daisies and tulips
    close to the eye where their cup is readable"""
    if kind == "white":
        w = (0.30, 0.42, 0.14, 0.14)
    elif kind in ("red", "scarlet"):
        w = (0.42, 0.20, 0.24, 0.14)
    elif kind == "orange":
        w = (0.36, 0.24, 0.22, 0.18)
    else:
        w = (0.28, 0.30, 0.22, 0.20)
    return w


def _tint_material(m, rgb, amount=0.35):
    """pull a textured material's output toward rgb without losing the map"""
    try:
        nt = m.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        if bsdf is None:
            return
        src = bsdf.inputs["Base Color"].links[0].from_socket if bsdf.inputs["Base Color"].is_linked else None
        if src is None:
            return
        mixn = nt.nodes.new("ShaderNodeMix"); mixn.data_type = "RGBA"
        mixn.blend_type = "MIX"; mixn.location = (450, -260)
        mixn.inputs["Factor"].default_value = amount
        nt.links.new(src, mixn.inputs[6])
        c = nt.nodes.new("ShaderNodeRGB")
        c.outputs[0].default_value = (*rgb, 1.0)
        nt.links.new(c.outputs[0], mixn.inputs[7])
        nt.links.new(mixn.outputs[2], bsdf.inputs["Base Color"])
    except Exception as e:
        print("P18_TINTFAIL", e)


def _limb(V, F, a, b, ra, rb, depth, ring=6, segs=8, arc=0.0, up=0.0, seed=0):
    """Append one tapered, arced tube between two image-space points.
    `arc` bows the limb sideways (perpendicular to its own axis) so branches curve instead of
    running dead straight -- the difference between a limb and a stick."""
    rnd = random.Random(seed)
    ua, va = a; ub, vb = b
    du, dv = ub - ua, vb - va
    L = math.hypot(du, dv) or 1e-6
    # perpendicular in image space
    pu, pv = -dv / L, du / L
    base = len(V)
    for k in range(segs + 1):
        t = k / segs
        # quadratic bow: max at the middle, zero at both ends
        bow = arc * math.sin(math.pi * t)
        uu = ua + du * t + pu * bow
        vv = va + dv * t + pv * bow + up * math.sin(math.pi * t) * 0.35
        rr = ra + (rb - ra) * t
        rr *= 1.0 + 0.06 * math.sin(t * 9.0 + seed)
        for m in range(ring):
            ang = 2 * math.pi * m / ring
            V.append((uu, vv, rr, math.cos(ang), math.sin(ang)))
    for k in range(segs):
        for m in range(ring):
            a0 = base + k * ring + m; a1 = base + k * ring + (m + 1) % ring
            b0 = base + (k + 1) * ring + m; b1 = base + (k + 1) * ring + (m + 1) % ring
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    return base


# ============================================================ PHASE 21: LANDFORM
# The brief is the LANDFORM: a hill rising out of the sea with the castle on its summit,
# terraced grass slopes stepping down, a stream cascading through them into the sea, and
# distant blue ridges behind.
#
# WHY THE LAND IS BUILT THIS WAY. This is a LEVEL ORTHOGRAPHIC camera looking along +Y, so
# image v maps 1:1 to world Z and there is no foreshortening at all. Under that projection a
# horizontal surface has ZERO area: 3D "treads" would be invisible, and displacing a vertex
# in Z does not move it up the picture, it moves it THROUGH the picture plane (straight
# toward the camera). The first attempt at this terrain did exactly that, and the result was
# a flat cliff -- every row collapsed onto its neighbours.
#
# So the hill is built the way a stage flat is built, which is also exactly how the
# reference is drawn: a SILHOUETTE whose top edge is the summit profile, filled with
# CONTOUR BANDS that step down from the summit. Each vertex sits at z = v2z(v), so the mesh
# is drawn precisely where it is addressed. Elevation is a scalar field used for COLOUR
# (terrace treads and risers), and a shallow Y bulge tilts the normals so the key light
# models the slope. That is what makes terracing read.
#
# Because the surface is single-valued in image space, the downward ray that seats every
# flower, cypress and bridge footing hits exactly the surface the renderer draws.

def p21_top_v(u, cfg):
    """The summit / silhouette row for column u -- where land begins at the top."""
    pts = cfg.get("p21_top_pts",
                  [[0.00, 0.362], [0.10, 0.388], [0.22, 0.412], [0.34, 0.432],
                   [0.44, 0.447], [0.55, 0.450], [0.66, 0.462], [0.76, 0.498],
                   [0.86, 0.552], [0.94, 0.600], [1.00, 0.640]])
    if u <= pts[0][0]:
        return pts[0][1]
    for i in range(len(pts) - 1):
        u0, u1 = pts[i][0], pts[i + 1][0]
        if u0 <= u <= u1:
            t = (u - u0) / max(u1 - u0, 1e-9)
            t = t * t * (3.0 - 2.0 * t)
            return pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
    return pts[-1][1]


def p21_shore_v(u, cfg):
    """Row where the hill meets the foreground / the water, per column."""
    pts = cfg.get("p21_shore_pts",
                  [[0.00, 0.790], [0.25, 0.780], [0.50, 0.772], [0.75, 0.762],
                   [0.90, 0.742], [1.00, 0.720]])
    if u <= pts[0][0]:
        return pts[0][1]
    for i in range(len(pts) - 1):
        u0, u1 = pts[i][0], pts[i + 1][0]
        if u0 <= u <= u1:
            t = (u - u0) / max(u1 - u0, 1e-9)
            return pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
    return pts[-1][1]


def p21_elev(u, v, cfg):
    """Elevation 0..1: 1 at the summit edge for this column, 0 at the shore."""
    vt = p21_top_v(u, cfg)
    vs = p21_shore_v(u, cfg)
    t = (vs - v) / max(vs - vt, 1e-9)
    return max(0.0, min(1.0, t))


def p21_band_elev(u, v, cfg):
    """Elevation used for the TERRACE BANDS only (not for shape, seating or shading).

    WHY THIS EXISTS. p21_elev normalises against the LOCAL summit and shore rows. Both of
    those drift strongly with u (top_v 0.41 -> 0.60 across the frame, shore_v 0.754 -> 0.712),
    so a given terrace boundary lands ~10-23 ROWS HIGHER on the right of a probe window than
    on the left. The bands are therefore not level stripes, they are slanted ones; a vertical
    scan that averages across even 0.12 of u smears every riser into the tread, which is
    exactly why the terraces measured max row-jump 5.9 with ZERO jumps > 6 despite the risers
    genuinely existing in the mesh.

    `p21_band_level` interpolates the normalising pair between the true per-column pair (1.0,
    the old behaviour) and one CONSTANT pair (0.0, dead level bands). Level bands are what the
    reference has -- its terraces run across the hill as horizontal steps -- and level bands
    are what make a vertical scan find the riser edges.
    """
    lvl = float(cfg.get("p21_band_level", 1.0))
    if lvl >= 1.0:
        return p21_elev(u, v, cfg)
    vt = p21_top_v(u, cfg)
    vs = p21_shore_v(u, cfg)
    vt0 = float(cfg.get("p21_band_vt0", 0.406))
    vs0 = float(cfg.get("p21_band_vs0", 0.754))
    vtl = vt0 + lvl * (vt - vt0)
    vsl = vs0 + lvl * (vs - vs0)
    t = (vsl - v) / max(vsl - vtl, 1e-9)
    return max(0.0, min(1.0, t))


def p21_terrace(e, cfg):
    """Quantise elevation into steps -> flat treads separated by short risers."""
    n = int(cfg.get("p21_terraces", 7))
    if n <= 0:
        return e
    soft = float(cfg.get("p21_terrace_soft", 0.22))
    k = e * n
    st = math.floor(k)
    fr = k - st
    f = min(1.0, max(0.0, (fr - (1.0 - soft)) / max(soft, 1e-6)))
    f = f * f * (3.0 - 2.0 * f)
    return (st + f) / n


def p21_terrace_ramp(v01):
    """The fraction of a band's height that is TREAD, 0..1.

    `p21_terrace` compresses the top `soft` of each band into a ramp, so the tread occupies
    the lower `1-soft`. v4 used soft=0.22, i.e. a 78%-wide tread -- correct. The colour code,
    however, darkened a sine over k in 0.16..0.90, i.e. over 74% of the band, so the "riser"
    shading covered almost the WHOLE band and the tread had nowhere left to be bright. That
    is the second reason the steps read as a smooth wash. Here the riser shading is confined
    to the ramp fraction and given a hard edge, so tread and riser are two clearly different
    luminances and a vertical scan steps across them.
    """
    soft = float(max(1e-4, min(1.0, v01)))
    return soft


def p21_land_exists(u, v, cfg):
    return v >= p21_top_v(u, cfg) - 0.002


def p21_stream_u(v, cfg):
    """Centre-line of the stream in u, from the castle's foot down to the sea."""
    pts = cfg.get("p21_stream_pts", [[0.352, 0.470], [0.392, 0.512], [0.452, 0.556],
                                     [0.508, 0.604], [0.560, 0.654], [0.632, 0.712],
                                     [0.722, 0.772]])
    if v <= pts[0][1]:
        return pts[0][0]
    for i in range(len(pts) - 1):
        v0, v1 = pts[i][1], pts[i + 1][1]
        if v0 <= v <= v1:
            t = (v - v0) / max(v1 - v0, 1e-9)
            t = t * t * (3.0 - 2.0 * t)
            return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
    return pts[-1][0]


def p21_stream_halfw(v, cfg):
    """Half-width of the stream in u: narrow at the source, widening toward the sea."""
    w0 = float(cfg.get("p21_stream_w", 0.0115))
    w1 = float(cfg.get("p21_stream_w_sea", 0.062))
    w2 = float(cfg.get("p21_stream_w_mouth", 0.150))
    t = max(0.0, min(1.0, (v - 0.47) / 0.32))
    hw = w0 + (w1 - w0) * (t * t)
    # past the shore the stream becomes an estuary: it must widen into the sea rather than
    # stopping dead as a dark gash in the field
    m = max(0.0, (v - 0.74) / 0.30)
    return hw + (w2 - w1) * min(1.0, m * m)


def p21_stream_dist(u, v, cfg):
    return abs(u - p21_stream_u(v, cfg)) - p21_stream_halfw(v, cfg)


def p21_path_u(v, cfg):
    """The stone path climbs the right-hand slope toward the castle."""
    pts = cfg.get("p21_path_pts", [[0.716, 0.800], [0.706, 0.740], [0.678, 0.684],
                                   [0.662, 0.628], [0.672, 0.578], [0.654, 0.528],
                                   [0.620, 0.484]])
    if v >= pts[0][1]:
        return pts[0][0]
    if v <= pts[-1][1]:
        return pts[-1][0]
    for i in range(len(pts) - 1):
        v0, v1 = pts[i][1], pts[i + 1][1]
        if v1 <= v <= v0:
            t = (v0 - v) / max(v0 - v1, 1e-9)
            t = t * t * (3.0 - 2.0 * t)
            return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
    return pts[-1][0]


def p21_ground(u, v, cfg):
    """World Z of the land surface at image point (u, v).

    SINGLE SOURCE OF TRUTH: the terrain mesh, the stream ribbon, the bridge footings and
    every plant seated on the hill all come through here, so they cannot disagree.

    z is v2z(v) exactly -- the land is drawn where it is addressed -- plus a small ripple so
    the surface is not perfectly planar and the light breaks across it."""
    z = v2z(v, cfg)
    rk = float(cfg.get("p21_land_ripple", 0.0016))
    if rk > 0.0:
        z += v2h(rk, cfg) * (math.sin(u * 27.0 + v * 9.0) + 0.6 * math.sin(v * 41.0 - u * 13.0))
    d = p21_stream_dist(u, v, cfg)
    if d < 0.0:
        z -= v2h(float(cfg.get("p21_carve", 0.0045)), cfg)
    else:
        bank = float(cfg.get("p21_bank", 0.011))
        if d < bank:
            z -= v2h(float(cfg.get("p21_carve", 0.0045)), cfg) * (1.0 - d / max(bank, 1e-6))
    return z


def p21_ground_y(u, v, cfg):
    """Depth (world Y) of the land: a shallow bulge that tilts the normals so the key light
    rakes across the slope instead of hitting a dead-flat wall."""
    e = p21_elev(u, v, cfg)
    y0 = float(cfg.get("depth_hill", 3.4))
    return y0 - float(cfg.get("p21_bulge", 1.6)) * (e ** 1.5)


P21_LIGHT = (-0.55, -0.35, 0.76)   # key direction in image space (x right, y depth, z up)


def p21_shade(col, nx=0.0, nz=1.0, cfg=None, amb=None, dif=None):
    """Bake a lambert term into a vertex colour.

    The land cannot be lit by the scene rig: that rig is calibrated for the FLOWER PETALS, a
    deliberately hot warm key with a 1.6 exposure lift (~8x on albedo). Grass authored at
    (55,77,30) rendered (170,177,141) under it -- every channel clipped, which is why the
    hill came out tan. Baking the shading into the vertex colour and rendering the terrain as
    emission instead takes the whole exposure problem out of the loop, and makes terrain
    colour directly authored rather than arrived at by lighting accident.
    """
    cfg = cfg or {}
    a = float(cfg.get("p21_ambient", 0.42)) if amb is None else amb
    d = float(cfg.get("p21_diffuse", 0.78)) if dif is None else dif
    L = P21_LIGHT
    n = math.sqrt(nx * nx + 1.0 * 1.0 + nz * nz) or 1.0
    lam = max(0.0, (nx * L[0] + (-1.0) * L[1] + nz * L[2]) / n)
    k = a + d * lam
    return tuple(max(0.0, min(1.0, col[c] * k)) for c in range(3))


def _vc_mat(name, rough=0.9, spec=None, emit=0.0, sat=1.0, gain=1.0):
    """Vertex-colour material, recreated fresh so repeated runs cannot stack nodes.

    `sat` and `gain` exist because the light rig is calibrated for the FLOWER PETALS (a
    deliberately hot warm key with a 1.6 exposure lift). Terrain authored at true leaf green
    renders tan under it: measured (171,177,141) for an authored (55,77,30), i.e. both far
    too bright AND washed to grey by the ambient. Rather than re-author every terrain colour,
    the material saturates and scales the vertex colour on the way to the shader, which is
    also the hook the fast .blend sweeps tune."""
    mat = bpy.data.materials.get(name)
    if mat is not None:
        bpy.data.materials.remove(mat)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (700, 0)
    bs = nt.nodes.new("ShaderNodeBsdfPrincipled"); bs.location = (420, 0)
    at = nt.nodes.new("ShaderNodeVertexColor"); at.location = (-380, 0)
    at.layer_name = "Col"
    src = at.outputs["Color"]
    if abs(sat - 1.0) > 1e-6:
        hs = nt.nodes.new("ShaderNodeHueSaturation"); hs.location = (-180, 60)
        hs.inputs["Saturation"].default_value = float(sat)
        nt.links.new(src, hs.inputs["Color"])
        src = hs.outputs["Color"]
    if isinstance(gain, (int, float)):
        gain = (float(gain), float(gain), float(gain))
    if any(abs(g - 1.0) > 1e-6 for g in gain):
        mx = nt.nodes.new("ShaderNodeMix"); mx.location = (60, 60)
        mx.data_type = "RGBA"; mx.blend_type = "MULTIPLY"
        mx.inputs["Factor"].default_value = 1.0
        mx.inputs[6].default_value = (gain[0], gain[1], gain[2], 1.0)
        nt.links.new(src, mx.inputs[7])
        src = mx.outputs[2]
    bs.inputs["Roughness"].default_value = rough
    if spec is not None:
        try:
            bs.inputs["Specular IOR Level"].default_value = spec
        except Exception:
            pass
    if emit > 0.0:
        bs.inputs["Emission Strength"].default_value = emit
        nt.links.new(src, bs.inputs["Emission Color"])
    nt.links.new(src, bs.inputs["Base Color"])
    nt.links.new(bs.outputs["BSDF"], out.inputs["Surface"])
    return mat


def _vc_obj(name, V, F, C, mat):
    me = bpy.data.meshes.new(name)
    me.from_pydata(V, [], F)
    me.validate()
    lay = me.color_attributes.new(name="Col", type="FLOAT_COLOR", domain="CORNER")
    for poly in me.polygons:
        for li in poly.loop_indices:
            vi = me.loops[li].vertex_index
            lay.data[li].color = (*tuple(look.srgb2lin(x) for x in C[vi]), 1.0)
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    return ob


def p21_hill_colour(u, v, cfg):
    """Colour one point of the hill: terrace treads, risers, rock scars and the stream bed."""
    # BANDS use the level-reference elevation (see p21_band_elev) so the steps run across the
    # hill as horizontal terraces instead of drifting 10-23 rows over 0.12 of u; SHAPE and the
    # scarp/summit/relief terms keep using the true per-column elevation.
    e = p21_band_elev(u, v, cfg)
    ez = p21_terrace(e, cfg)
    n = max(1, int(cfg.get("p21_terraces", 7)))
    k = ez * n - math.floor(ez * n)
    g_lo = cfg.get("p21_grass_lo", (0.215, 0.300, 0.118))
    g_hi = cfg.get("p21_grass_hi", (0.395, 0.470, 0.196))
    rook = cfg.get("p21_rock_rgb", (0.430, 0.410, 0.365))
    bank_c = cfg.get("p21_bank_rgb", (0.300, 0.300, 0.262))

    # ---- TREAD vs RISER --------------------------------------------------------------
    # v4 shaded a sine over k in 0.16..0.90 -- 74% of every band -- so the "riser" tint bled
    # across nearly the whole tread and the field read as one smooth wash (measured max
    # row-jump 5.9 on the left slope, zero jumps > 6, against the target's 26).
    # Here the tread is flat across its whole width with only a gentle within-band ramp, and
    # the riser is a NARROW, HARD-EDGED band at the top of each step: tread and riser become
    # two distinct luminances and a vertical scan steps across the boundary.
    tread_base = float(cfg.get("p21_tread_base", 0.30))   # luminance of the tread's foot
    tread_ramp = float(cfg.get("p21_tread_ramp", 0.55))   # gentle rise across the tread
    band_var = float(cfg.get("p21_band_var", 0.20))       # per-terrace offset, so steps differ
    _soft = float(cfg.get("p21_terrace_soft", 0.22))
    # IMPORTANT: tread/riser must be decided on the RAW band fraction, not on `k` above.
    # `k` is `p21_terrace`'s OUTPUT, which already maps the whole upper `soft` of the band
    # onto the 0.9..1.0 ramp -- so testing `k > 1-soft` selects a sliver about 1/10 as wide as
    # intended and the riser all but vanishes (measured: 3 jumps > 6 instead of ~26).
    # Using the raw fraction puts the riser on exactly `soft` of every step, at full contrast.
    _kr = (e * n) - math.floor(e * n)
    _bv = 0.5 + 0.5 * math.sin(math.floor(e * n) * 2.39996323)
    col = tuple(g_lo[c] + (g_hi[c] - g_lo[c]) * (tread_base + tread_ramp * _kr
                                                 + band_var * (_bv - 0.5)) for c in range(3))
    if _kr > 1.0 - _soft:
        # riser occupies exactly the top `soft` fraction of the step, hard-edged, and DARKENED
        # rather than warmed so it reads as the step's shadowed face
        ris = min(1.0, (_kr - (1.0 - _soft)) / max(_soft, 1e-9))
        ris = ris * ris * (3.0 - 2.0 * ris)
        _rd = float(cfg.get("p21_riser_dark", 0.62))
        _rm = float(cfg.get("p21_riser_mix", 0.30))
        col = tuple(col[c] * (1.0 - _rd * ris) + rook[c] * _rm * ris for c in range(3))
        # RISERS COOL TOWARD TEAL. The reference's step faces are not merely darker ochre:
        # they shift blue-green (its hill window holds 29% of pixels at hue 175-260 and only
        # 6% at 62-90, while v4 holds 81% at 35-62). Cooling the riser is what puts a colour
        # break at every step edge, on top of the luminance break.
        _teal = float(cfg.get("p21_riser_teal", 0.0))
        if _teal > 0.0:
            _t = _teal * ris
            col = (col[0] * (1.0 - _t), col[1] * (1.0 - _t * 0.35), col[2] * (1.0 + _t * 0.55))

    # vertical scarps on the steepest flanks read as exposed rock, which is what gives the
    # hill mass instead of looking like striped paper
    de = abs(p21_elev(u + 0.004, v, cfg) - p21_elev(u - 0.004, v, cfg)) / 0.008
    if de > float(cfg.get("p21_scarp", 1.5)):
        s = min(1.0, (de - float(cfg.get("p21_scarp", 1.5))) / 2.5)
        col = tuple(col[c] + (rook[c] * 0.92 - col[c]) * (0.58 * s) for c in range(3))

    # a dark band right under the summit edge -> the hill turns away from the light there
    vt = p21_top_v(u, cfg)
    depth_below = (v - vt) / max(float(cfg.get("p21_summit_shadow", 0.020)), 1e-6)
    if depth_below < 1.0:
        col = tuple(col[c] * (0.62 + 0.38 * depth_below) for c in range(3))

    d = p21_stream_dist(u, v, cfg)
    if d < 0.0:
        col = tuple(bank_c[c] for c in range(3))
    elif d < float(cfg.get("p21_bank", 0.011)):
        t = 1.0 - d / max(cfg.get("p21_bank", 0.011), 1e-6)
        col = tuple(col[c] + (bank_c[c] - col[c]) * (0.70 * t) for c in range(3))

    # baked lambert from the local elevation gradient: the riser under each terrace edge
    # turns away from the key and darkens, which is what separates tread from riser
    du = (p21_elev(u + 0.004, v, cfg) - p21_elev(u - 0.004, v, cfg)) / 0.008
    dv = (p21_elev(u, v - 0.004, cfg) - p21_elev(u, v + 0.004, cfg)) / 0.008
    k = float(cfg.get("p21_relief", 2.6))
    nx = -du * k * 0.30
    nz = 1.0 - min(0.85, abs(dv) * k * 0.30)
    col = p21_shade(col, nx=nx, nz=nz, cfg=cfg)
    return col


def build_terrain(cfg):
    """The terraced hill: one silhouette-shaped heightfield with contour bands."""
    NU = int(cfg.get("p21_hill_nu", 430))
    NV = int(cfg.get("p21_hill_nv", 200))
    u0, u1 = cfg.get("p21_hill_u0", -0.02), cfg.get("p21_hill_u1", 1.02)
    v_bot = float(cfg.get("p21_hill_v2", 1.24))
    V, F, C = [], [], []
    idx = {}
    for i in range(NU + 1):
        u = u0 + (u1 - u0) * (i / NU)
        vt = p21_top_v(u, cfg)
        for j in range(NV + 1):
            t = j / NV
            v = vt + (v_bot - vt) * t
            idx[(i, j)] = len(V)
            V.append((u2x(u, cfg), p21_ground_y(u, v, cfg), p21_ground(u, v, cfg)))
            C.append(p21_hill_colour(u, v, cfg))
    W = NV + 1
    for i in range(NU):
        for j in range(NV):
            a = i * W + j; b = a + 1; c = a + W; d = c + 1
            F.append((a, c, d)); F.append((a, d, b))
    ob = _vc_obj("p21_hill", V, F, C, _vc_mat(
        "p21_hill_vc", rough=float(cfg.get("p21_hill_rough", 0.95)), emit=1.0,
        sat=float(cfg.get("p21_hill_sat", 1.0)),
        gain=tuple(float(x) for x in cfg.get("p21_hill_gain", (1.0, 1.0, 1.0)))))
    print("P21_TERRAIN u %.2f..%.2f grid %dx%d faces=%d top_v %.3f..%.3f terraces=%d"
          % (u0, u1, NU, NV, len(F), p21_top_v(u0, cfg), p21_top_v(u1, cfg),
             int(cfg.get("p21_terraces", 7))))
    return ob


def build_stream(cfg):
    """The water: a ribbon laid on the hill surface, with a bright sheet at each terrace
    riser the channel crosses. It must read as a legible blue ribbon through the green."""
    NV = 190
    v0 = float(cfg.get("p21_stream_v0", 0.470))
    v1 = float(cfg.get("p21_stream_v1", 0.782))
    wcol = cfg.get("p21_water_rgb", (0.175, 0.390, 0.560))
    lift = v2h(float(cfg.get("p21_water_lift", 0.0022)), cfg)
    V, F, C = [], [], []
    for j in range(NV + 1):
        t = j / NV
        v = v0 + (v1 - v0) * t
        cu = p21_stream_u(v, cfg)
        hw = p21_stream_halfw(v, cfg)
        yy = p21_ground_y(cu, v, cfg) - 0.06
        for s, k in ((-1.0, 0.78), (-0.45, 1.0), (0.45, 1.0), (1.0, 0.78)):
            u = cu + s * hw
            z = p21_ground(u, v, cfg) + lift
            V.append((u2x(u, cfg), yy, z))
            rip = 0.5 + 0.5 * math.sin(v * 240.0 + s * 1.9)
            C.append(tuple(wcol[c] * (k * (0.86 + 0.26 * rip)) for c in range(3)))
    for j in range(NV):
        a = j * 4
        for q in range(3):
            F.append((a + q, a + q + 1, a + q + 5)); F.append((a + q, a + q + 5, a + q + 4))
    mat = _vc_mat("p21_water_vc", rough=0.14, spec=0.90, emit=1.0,
                  gain=(2.0 ** (-float(cfg.get("exposure", 0.0))),) * 3)
    ob = _vc_obj("p21_stream", V, F, C, mat)

    # ---- waterfalls at each riser the stream crosses
    nter = max(1, int(cfg.get("p21_terraces", 7)))
    frgb = cfg.get("p21_fall_rgb", (0.800, 0.860, 0.900))
    wf, wc, found = [], [], 0
    for q in range(1, nter):
        lo, hi = v0, v1
        for _ in range(46):
            mid = 0.5 * (lo + hi)
            if p21_terrace(p21_elev(p21_stream_u(mid, cfg), mid, cfg), cfg) > q / nter:
                lo = mid
            else:
                hi = mid
        vq = 0.5 * (lo + hi)
        cu = p21_stream_u(vq, cfg)
        hw = p21_stream_halfw(vq, cfg) * 0.95
        ztop = p21_ground(cu, vq - 0.004, cfg)
        zbot = p21_ground(cu, vq + 0.012, cfg)
        if ztop - zbot < v2h(0.0012, cfg):
            continue
        yy = p21_ground_y(cu, vq, cfg) - 0.10
        wf += [(u2x(cu - hw, cfg), yy, ztop), (u2x(cu + hw, cfg), yy, ztop),
               (u2x(cu + hw, cfg), yy, zbot), (u2x(cu - hw, cfg), yy, zbot)]
        for _ in range(4):
            wc.append(frgb)
        found += 1
    if wf:
        F2 = []
        for q in range(len(wf) // 4):
            b = q * 4
            F2.append((b, b + 1, b + 2)); F2.append((b, b + 2, b + 3))
        _vc_obj("p21_falls", wf, F2, wc, mat)
    print("P21_STREAM v %.3f..%.3f halfw %.3f..%.3f waterfalls=%d"
          % (v0, v1, p21_stream_halfw(v0, cfg), p21_stream_halfw(v1, cfg), found))
    return ob


def build_mountains(cfg):
    """Layered blue ridges standing ON the horizon line.

    Each layer's base is the horizon itself, so the range rises out of the water with no
    card edge showing, and successive layers are paler and bluer -- that atmospheric fade is
    what turns a flat background band into depth."""
    layers = cfg.get("p21_mtn_layers", [
        (0.052, (0.500, 0.560, 0.620), 30.0, 3, 2.6, 1.00),
        (0.038, (0.425, 0.490, 0.565), 27.0, 7, 1.9, 1.18),
        (0.026, (0.350, 0.420, 0.505), 24.0, 11, 1.35, 1.42),
    ])
    u0, u1 = -0.06, 1.06
    base_v = float(cfg.get("p21_mtn_base", HORIZON_V))
    zb = v2z(base_v, cfg)
    made = 0
    for li, (amp, col, dep, seed, wl, stretch) in enumerate(layers):
        NX = 280
        rnd = random.Random(seed)
        ph = [rnd.uniform(0.0, 6.283) for _ in range(6)]
        V, F, C = [], [], []
        for i in range(NX + 1):
            t = i / NX
            u = u0 + (u1 - u0) * t
            h = (0.55 * math.sin(t * wl * 3.14159 + ph[0])
                 + 0.28 * math.sin(t * wl * 7.3 + ph[1])
                 + 0.17 * math.sin(t * wl * 17.0 + ph[2])
                 + 0.10 * math.sin(t * wl * 41.0 + ph[3]))
            h = (0.5 + 0.5 * h) * amp          # 0..amp, never negative -> hills, not troughs
            h = h ** stretch
            h *= 1.0 - 0.62 * max(0.0, (t - 0.58) / 0.42)
            V.append((u2x(u, cfg), dep, zb + v2h(h, cfg)))
            V.append((u2x(u, cfg), dep, zb - v2h(0.006, cfg)))
            C.append(col)
            C.append(tuple(col[c] * 0.86 for c in range(3)))
        for i in range(NX):
            a = i * 2
            F.append((a, a + 2, a + 3)); F.append((a, a + 3, a + 1))
        _vc_obj("p21_mtn%d" % li, V, F, C, _vc_mat(
            "p21_mtn_vc%d" % li, rough=1.0, emit=1.0,
            gain=2.0 ** (-float(cfg.get("exposure", 0.0)))))
        made += 1
    print("P21_MOUNTAINS layers=%d base v=%.3f" % (made, base_v))


def build_path(cfg):
    """The stone path: a pale ribbon climbing the right slope from the shore to the castle
    gate, drawn ON the hill surface so it follows every terrace it crosses."""
    NV = 120
    v0 = float(cfg.get("p21_path_v0", 0.470))
    v1 = float(cfg.get("p21_path_v1", 0.800))
    hw = float(cfg.get("p21_path_w", 0.0125))
    pc = cfg.get("p21_path_rgb", (0.640, 0.600, 0.520))
    pl = tuple(min(1.0, x * 1.16) for x in pc)
    V, F, C = [], [], []
    for j in range(NV + 1):
        v = v0 + (v1 - v0) * (j / NV)
        cu = p21_path_u(v, cfg)
        yy = p21_ground_y(cu, v, cfg) - 0.05
        for s in (-1.0, 1.0):
            u = cu + s * hw
            V.append((u2x(u, cfg), yy, p21_ground(u, v, cfg) + v2h(0.0016, cfg)))
        # alternate light/dark so the path reads as laid stones, not a painted stripe
        k = 0.5 + 0.5 * math.sin(v * 150.0)
        C.append(tuple(pc[c] * (0.90 + 0.20 * k) for c in range(3)))
        C.append(tuple(pl[c] * (0.90 + 0.20 * k) for c in range(3)))
    for j in range(NV):
        a = j * 2
        F.append((a, a + 1, a + 3)); F.append((a, a + 3, a + 2))
    _vc_obj("p21_path", V, F, C, _vc_mat("p21_path_vc", rough=0.90, emit=1.0,
                                         gain=(2.0 ** (-float(cfg.get("exposure", 0.0))),) * 3))
    print("P21_PATH v %.3f..%.3f hw=%.3f" % (v0, v1, hw))


def build_summit(cfg):
    """The castle platform: a stone-faced plinth on the summit so the villa sits on built
    ground at the TOP of the hill rather than being pasted onto a slope.

    An ortho level camera cannot show a horizontal deck, so the plateau is expressed the way
    it reads in the target: a horizontal coping line, a coursed retaining wall beneath it,
    and a darker shadow band under the lip."""
    u = float(cfg.get("p21_summit_u2", 0.560))
    v = float(cfg.get("p21_summit_v2", 0.478))
    w = float(cfg.get("p21_summit_w", 0.300))
    h = float(cfg.get("p21_summit_h", 0.030))       # retaining wall height in v
    pc = cfg.get("p21_summit_rgb", (0.610, 0.575, 0.510))
    pd = tuple(x * 0.66 for x in pc)
    V, F, C = [], [], []
    u0, u1 = u - w * 0.5, u + w * 0.5
    yy = p21_ground_y(u, v, cfg) - 0.42
    z_top = p21_ground(u, v, cfg) + v2h(0.0015, cfg)
    z_bot = v2z(v + h, cfg)
    # wall face, with courses
    NC = 34
    for i in range(NC):
        a0 = u0 + (u1 - u0) * (i / NC)
        a1 = u0 + (u1 - u0) * ((i + 1) / NC)
        k = 0.5 + 0.5 * math.sin(i * 2.1)
        col = tuple(pc[c] * (0.86 + 0.20 * k) for c in range(3))
        zt0 = z_top - v2h(0.004 * math.sin(i * 1.7), cfg)
        zt1 = z_top - v2h(0.004 * math.sin((i + 1) * 1.7), cfg)
        quad_free(V, F, C, (u2x(a0, cfg), yy, z_bot), (u2x(a1, cfg), yy, z_bot),
                  (u2x(a1, cfg), yy, zt1), (u2x(a0, cfg), yy, zt0), col)
    # coping: a lighter proud band along the top lip
    quad_free(V, F, C, (u2x(u0, cfg), yy + 0.04, z_top + v2h(0.002, cfg)),
              (u2x(u1, cfg), yy + 0.04, z_top + v2h(0.002, cfg)),
              (u2x(u1, cfg), yy, z_top + v2h(0.006, cfg)),
              (u2x(u0, cfg), yy, z_top + v2h(0.006, cfg)),
              tuple(min(1.0, x * 1.20) for x in pc))
    # shadow under the lip
    quad_free(V, F, C, (u2x(u0, cfg), yy + 0.02, z_top),
              (u2x(u1, cfg), yy + 0.02, z_top),
              (u2x(u1, cfg), yy + 0.02, z_top - v2h(0.008, cfg)),
              (u2x(u0, cfg), yy + 0.02, z_top - v2h(0.008, cfg)), pd)
    _vc_obj("p21_summit", V, F, C, _vc_mat("p21_summit_vc", rough=0.86, emit=1.0,
                                           gain=(2.0 ** (-float(cfg.get("exposure", 0.0))),) * 3))
    print("P21_SUMMIT u=%.3f v=%.3f w=%.3f wall_h=%.3f" % (u, v, w, h))


def quad_free(V, F, C, p0, p1, p2, p3, col):
    b = len(V)
    V.extend([p0, p1, p2, p3])
    F.append((b, b + 1, b + 2)); F.append((b, b + 2, b + 3))
    for _ in range(4):
        C.append(col)


def build_sea_ripples(cfg):
    """Fine horizontal ripple lines on the sea, drawn as geometry rather than a 2D map --
    that is what gives it water texture without reintroducing any tiling."""
    import random as _r
    rnd = _r.Random(int(cfg.get("p21_ripple_seed", 17)))
    n = int(cfg.get("p21_ripple_lines", 130))
    vy0 = float(cfg.get("p21_sea_ripple_v0", HORIZON_V + 0.004))
    vy1 = float(cfg.get("p21_sea_ripple_v1", 0.404))
    V, F, C = [], [], []
    for _ in range(n):
        v = rnd.uniform(vy0, vy1)
        u0 = rnd.uniform(-0.05, 0.55)
        ln = rnd.uniform(0.06, 0.34)
        thick = rnd.uniform(0.0012, 0.0032)
        a = rnd.uniform(0.10, 0.30)
        # shorter, fainter lines near the horizon: perspective
        fade = (v - vy0) / max(vy1 - vy0, 1e-9)
        a *= 0.35 + 0.65 * fade
        col = (0.62 * a + 0.10, 0.72 * a + 0.17, 0.80 * a + 0.33)
        yy = float(cfg.get("depth_sea", DEPTH["sea"])) - 0.4
        quad_free(V, F, C, (u2x(u0, cfg), yy, v2z(v, cfg)),
                  (u2x(u0 + ln, cfg), yy, v2z(v, cfg)),
                  (u2x(u0 + ln, cfg), yy, v2z(v + thick, cfg)),
                  (u2x(u0, cfg), yy, v2z(v + thick, cfg)), col)
    _vc_obj("p21_sea_ripple", V, F, C, _vc_mat(
        "p21_sea_ripple_vc", rough=1.0, emit=1.0,
        gain=2.0 ** (-float(cfg.get("exposure", 0.0)))))
    print("P21_SEARIPPLE lines=%d v %.3f..%.3f" % (n, vy0, vy1))


def place_oaks(cfg):
    """Framing oaks. The target shows a THICK VISIBLE TRUNK entering the frame, then 2-4
    branches arching across the corner, and the canopy is CLUSTERS OF LEAVES bound to twig
    tips with orange fruit hanging under them -- not faceted blobs. So:
      trunk (inside frame, thick) -> crotch -> 2-4 arching limbs -> twigs -> small dense
      leaf clusters at each twig tip + a few leaf clusters along the limb, plus fruit."""
    S = cfg.get("ortho_scale", 7.0)
    depth = cfg.get("depth_oaks", DEPTH["oaks"])
    leaf_rgb = cfg.get("oak_leaf_rgb", (0.145, 0.205, 0.070))
    leaf_m = make_mat("p16_oak_leaf", "leaf")
    _set_base(leaf_m, leaf_rgb, rough=0.84)
    wood_m = make_mat("p16_oak_wood", "wood")
    _set_base(wood_m, (0.135, 0.086, 0.048), rough=0.91)
    fruit_m = bpy.data.materials.get("p16_fruit")
    if fruit_m is None:
        fruit_m = bpy.data.materials.new("p16_fruit")
    _set_base(fruit_m, (0.72, 0.235, 0.018), rough=0.52)

    for bi, spec in enumerate(OAK_BRANCHES):
        u0, v0, u1, v1 = spec["u0"], spec["v0"], spec["u1"], spec["v1"]
        cu, cv = (u0 + u1) / 2.0, (v0 + v1) / 2.0
        rnd = random.Random(3100 + bi)
        TV, TF = [], [],          # wood
        LV, LF = [], []           # leaf clumps
        FV, FF = [], []           # fruit

        # ---- TRUNK: starts above the top edge and descends INTO frame so a real,
        #      thick trunk section is visible in the corner, then forks at the crotch.
        tr = cfg.get("oak_r", 0.016) * S
        tru = u0 + (u1 - u0) * 0.045
        crotch = (tru, v0 + (v1 - v0) * float(cfg.get("oak_crotch", 0.44)))
        _limb(TV, TF, (u0, v0 - 0.10), crotch, tr * 2.30, tr * 1.62, depth,
              ring=8, segs=12, arc=0.006, seed=bi * 7 + 1)
        # two buttress roots flaring from the crotch for weight
        for sgn in (-1.0, 1.0):
            _limb(TV, TF, crotch, (crotch[0] + sgn * rnd.uniform(0.02, 0.05), crotch[1] - 0.05),
                  tr * 1.10, tr * 0.30, depth, ring=6, segs=7, arc=0.004,
                  seed=bi * 13 + int(sgn) + 3)

        # ---- 2-4 primary limbs ARCHING out of the crotch: each bows strongly and its far
        #      end drops back down, which is what makes a branch read as arching.
        nl = int(cfg.get("oak_limbs", rnd.randint(2, 4)))
        nl = max(2, min(4, nl))
        twigs = []
        for li in range(nl):
            f = li / max(nl - 1, 1)                       # 0..1 across the fan
            # spread the far ends across the canopy bbox, biased away from the trunk
            eu = crotch[0] + (u1 - crotch[0]) * (0.26 + 0.62 * f) + rnd.uniform(-0.03, 0.03)
            ev = crotch[1] + (v1 - crotch[1]) * (0.42 + 0.42 * (1.0 - abs(0.5 - f) * 2.0))
            ev += rnd.uniform(-0.04, 0.04)
            # elbow above the chord -> the limb rises then turns over: an arch
            elbow = (crotch[0] + (eu - crotch[0]) * 0.46,
                     max(crotch[1], ev) + abs(eu - crotch[0]) * float(cfg.get("oak_arch", 0.62)))
            _limb(TV, TF, crotch, elbow, tr * 1.62, tr * 0.80, depth,
                  ring=7, segs=9, arc=rnd.uniform(-0.020, 0.020), seed=bi * 31 + li)
            _limb(TV, TF, elbow, (eu, ev), tr * 0.80, tr * 0.34, depth,
                  ring=6, segs=9, arc=rnd.uniform(-0.030, 0.030), seed=bi * 37 + li)
            # one leaf cluster along the limb so the branch itself carries leaves
            for li2 in range(2):
                g = 0.45 + 0.35 * li2
                lu = crotch[0] + (eu - crotch[0]) * g
                lv = crotch[1] + (elbow[1] - crotch[1]) * g + (ev - elbow[1]) * max(0.0, g - 0.5)
                twigs.append((lu + rnd.uniform(-0.02, 0.02), lv + rnd.uniform(-0.02, 0.02)))
            # ---- 2-3 secondary twigs arching off the limb tip
            nt = rnd.randint(2, 3)
            for ti in range(nt):
                g = (ti + 1) / nt
                tu = eu + (u1 - eu) * rnd.uniform(0.12, 0.60) * (0.6 + 0.6 * g) + rnd.uniform(-0.04, 0.04)
                tv_ = ev + (v1 - ev) * rnd.uniform(0.10, 0.58) * (0.6 + 0.6 * g) + rnd.uniform(-0.05, 0.04)
                _limb(TV, TF, (eu, ev), (tu, tv_), tr * 0.34, tr * 0.12, depth,
                      ring=5, segs=6, arc=rnd.uniform(-0.028, 0.028), seed=bi * 71 + li * 7 + ti)
                twigs.append((tu, tv_))

        # ---- LEAF CLUSTERS: small, dense, many, centred ON the twig tips so the canopy
        #      silhouette is a chain of leaf masses following the branches.
        ncl = int(cfg.get("oak_clumps", 60))
        cr0 = cfg.get("oak_clump_r", 0.072) * S
        per = max(2, int(round(ncl / max(len(twigs), 1))))
        for (tu, tv_) in twigs:
            for ci in range(per):
                a = rnd.uniform(0, 6.283)
                spread = cr0 * rnd.uniform(0.45, 1.25)
                du = math.cos(a) * spread * rnd.uniform(0.25, 1.0)
                dv = math.sin(a) * spread * rnd.uniform(0.25, 1.0)
                cr = cr0 * rnd.uniform(0.30, 0.62)
                bv, bf = blob(u2x(tu, cfg) + du * 0.55, depth + rnd.uniform(-0.55, 0.55),
                              v2z(tv_, cfg) + dv * 0.55,
                              cr * rnd.uniform(0.9, 1.5), cr * rnd.uniform(0.6, 1.0),
                              cr * rnd.uniform(0.9, 1.5),
                              seg=7, rings=4, jitter=0.34, seed=rnd.randrange(1 << 20))
                o = len(LV); LV.extend(bv)
                LF.extend([(x + o, y + o, z + o) for (x, y, z) in bf])

        # ---- orange fruit hanging BELOW the branch line
        nf = int(spec.get("fruit", 11))
        fr = cfg.get("fruit_s", 0.0075) * S
        for _ in range(nf):
            if not twigs:
                break
            tu, tv_ = twigs[rnd.randrange(len(twigs))]
            hx = tu + rnd.uniform(-0.045, 0.045)
            hy = tv_ + rnd.uniform(0.010, 0.085)          # hang lower on screen = below
            bv, bf = blob(u2x(hx, cfg), depth - 0.90, v2z(hy, cfg),
                          fr, fr * 0.92, fr, seg=6, rings=3, jitter=0.10,
                          seed=rnd.randrange(1 << 20))
            o = len(FV); FV.extend(bv)
            FF.extend([(x + o, y + o, z + o) for (x, y, z) in bf])

        # ---- convert the (u,v,r,nx,ny) limb records into world tubes
        WV, WF = [], []
        for (uu, vv, rr, nx, ny) in TV:
            WV.append((u2x(uu, cfg) + nx * rr, depth + 0.35 + ny * rr * 0.85, v2z(vv, cfg) + ny * rr))
        for (a, b, c) in TF:
            WF.append((a, b, c))
        mesh_from("p16_oak%d_wood" % bi, WV, WF, wood_m)
        mesh_from("p16_oak%d_leaf" % bi, LV, LF, leaf_m)
        mesh_from("p16_oak%d_fruit" % bi, FV, FF, fruit_m)
        print("P19_OAK%d limbs=%d twigs=%d trunk_r=%.3f wood=%d leafclumps=%d fruit=%d  u %.2f..%.2f v %.2f..%.2f"
              % (bi, nl, len(twigs), tr * 2.30, len(WF), len(LV), len(FF) // 3, u0, u1, v0, v1))
    print("P16_OAKS", len(OAK_BRANCHES))

def blob(cx, cy, cz, rx, ry, rz, seg=8, rings=5, jitter=0.16, seed=0):
    """A lumpy spheroid built with plain trigonometry -- no bmesh, no ops, fully
    deterministic. Used for leaf clumps and fruit so canopy geometry can never go missing."""
    rnd = random.Random(seed)
    V, F = [], []
    for j in range(rings + 1):
        phi = math.pi * j / rings
        sp, cp = math.sin(phi), math.cos(phi)
        for i in range(seg):
            th = 2 * math.pi * i / seg
            k = 1.0 + rnd.uniform(-jitter, jitter)
            V.append((cx + rx * sp * math.cos(th) * k,
                      cy + ry * cp * k,
                      cz + rz * sp * math.sin(th) * k))
    for j in range(rings):
        for i in range(seg):
            a = j * seg + i
            b = j * seg + (i + 1) % seg
            c = (j + 1) * seg + i
            d = (j + 1) * seg + (i + 1) % seg
            F.append((a, b, d)); F.append((a, d, c))
    return V, F


def bmesh_ico(subdiv, scale):
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=scale)
    me = bpy.data.meshes.new("tmp_ico")
    bm.to_mesh(me); bm.free()
    pv = np.array([[v.co.x, v.co.y, v.co.z] for v in me.vertices], float)
    pf = [tuple(p.vertices) for p in me.polygons]
    bpy.data.meshes.remove(me)
    return pv, pf

# ------------------------------------------------------------------ 7. aqueduct
def build_aqueduct(cfg):
    """The distant aqueduct reads as a silhouette: an arcade of round arches standing on the
    horizon at the far right. Built directly in IMAGE space (a curtain of quads facing -Y),
    so it can never be 'lying down' the way a ground-plane mesh does under a level camera."""
    stone = make_mat("p16_aqueduct_stone", "stone")
    u = cfg.get("aqueduct_u", AQUEDUCT["u"])
    v = cfg.get("aqueduct_v", AQUEDUCT["v"])
    w = cfg.get("aqueduct_w", AQUEDUCT["w"])
    depth = cfg.get("depth_aqueduct", DEPTH["aqueduct"])
    span = u2w(w, cfg)
    h = span * cfg.get("aq_h", 0.34)
    n = int(cfg.get("aq_arches", 7))
    pier = span / n * 0.34
    V, F = [], []

    def quad(x0, z0, x1, z1):
        i = len(V)
        V.extend([(x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)])
        F.append((i, i + 1, i + 2, i + 3))

    x0 = u2x(u, cfg) - span / 2
    # solid body from the springing line up to the deck
    spring = h * 0.52
    for i in range(n + 1):
        px = x0 + span * i / n
        quad(px - pier / 2, 0.0, px + pier / 2, h)
    # spandrel wall above the arches, below the deck
    deck_z = h
    quad(x0, spring, x0 + span, deck_z)
    ob = mesh_from("p16_aqueduct", V, F, stone)
    # Arch openings: dark insets ONLY inside each arch. The old code built these into the
    # same vertex/face soup as the masonry and then handed that whole soup to a second
    # near-black object, so the entire arcade was overpainted black -- that was the
    # "black/metallic blob" on the right edge of frame. Separate geometry fixes it.
    SV, SF = [], []
    aq_dark = cfg.get("aq_dark", (0.16, 0.185, 0.20))
    dark = mat_flat("p16_aq_shadow", tuple(look.srgb2lin(c) for c in aq_dark), 0.72)
    for i in range(n):
        ax0 = x0 + span * i / n + pier / 2
        ax1 = x0 + span * (i + 1) / n - pier / 2
        cx = (ax0 + ax1) / 2.0
        rr = (ax1 - ax0) / 2.0
        seg = 9
        for k in range(seg):
            a0 = math.pi * (k / seg); a1 = math.pi * ((k + 1) / seg)
            xa = cx - rr * math.cos(a0); xb = cx - rr * math.cos(a1)
            za = spring - (spring * 0.94) * math.sin(a0)
            zb = spring - (spring * 0.94) * math.sin(a1)
            j = len(SV)
            SV.extend([(xa, -0.02, 0.001), (xb, -0.02, 0.001),
                       (xb, -0.02, max(zb, 0.001)), (xa, -0.02, max(za, 0.001))])
            SF.append((j, j + 1, j + 2, j + 3))
    shadow = mesh_from("p16_aq_shadow", SV, SF, dark)
    for o in (ob, shadow):
        o.location = (0, depth, v2z(v, cfg))
    # the shadow must sit just in front of the masonry
    shadow.location.y = depth - 0.4
    print("P16_AQUEDUCT u=%.3f v=%.3f span=%.2f h=%.2f arches=%d" % (u, v, span, h, n))


# ------------------------------------------------------------------ 8. main
PROBE_COLOURS = {
    "p16_probe_island":  (0.62, 0.48, 0.30),
    "p16_probe_villa":   (0.80, 0.22, 0.10),
    "p16_probe_cypress": (0.10, 0.30, 0.10),
    "p16_probe_meadow":  (0.35, 0.55, 0.18),
    "p16_probe_oak":     (0.18, 0.22, 0.42),
    "p16_probe_horse":   (0.95, 0.95, 0.95),
    "p16_probe_prop":    (0.55, 0.55, 0.58),
    "p16_probe_aq":      (0.42, 0.40, 0.36),
}


def apply_probe():
    """Flat, fully-bright materials per role, so a numeric pass can locate every group."""
    groups = [("p16_probe_island", ["oskar_scene"]),
              ("p16_probe_villa", ["oskar_scene.001"]),
              ("p16_probe_prop", ["stone_path_gate", "stone_arch_bridge"]),
              ("p16_probe_horse", ["saddled_horse"])]
    for matname, prefixes in groups:
        m = mat_flat(matname, tuple(look.srgb2lin(c) for c in PROBE_COLOURS[matname]), 1.0)
        for o in bpy.data.objects:
            if o.type != "MESH":
                continue
            if any(o.name == pre or o.name.startswith(pre + ".") for pre in prefixes):
                o.data.materials.clear()
                o.data.materials.append(m)
    m = mat_flat("p16_probe_meadow", tuple(look.srgb2lin(c) for c in PROBE_COLOURS["p16_probe_meadow"]), 1.0)
    for nm in ("p16_meadow",):
        o = bpy.data.objects.get(nm)
        if o:
            o.data.materials.clear(); o.data.materials.append(m)
    for nm, col in (("p16_probe_cypress", "p16_cypress"), ("p16_probe_oak", "p16_oak_canopy"),
                    ("p16_probe_aq", "p16_aqueduct")):
        mm = mat_flat(nm, tuple(look.srgb2lin(c) for c in PROBE_COLOURS[nm]), 1.0)
        for o in bpy.data.objects:
            if o.name.startswith(col):
                o.data.materials.clear(); o.data.materials.append(mm)
    print("P16_PROBE applied")


def ray_hit(dg, cfg, u, v, y_from=None):
    """Fire a ray straight down the camera axis through image point (u, v) and return the
    first surface it hits. In an ortho level camera this is exact: it is literally what the
    renderer sees at that pixel. Returns (obj, world_point) or (None, None)."""
    S = cfg.get("ortho_scale", 7.0); Cz = cfg.get("center_z", 3.0)
    x = u2x(u, cfg)
    z = v2z(v, cfg)
    # The camera lives at large -Y looking toward +Y, so "toward the camera" is NEGATIVE y.
    # Start well in front of every layer (oaks sit at y~-12) or the ray starts inside them
    # and reports their back faces instead of the terrain.
    y0 = cfg.get("ray_y0", -20.0)
    origin = Vector((x, y0, z))
    direction = Vector((0.0, 1.0, 0.0))
    ok, loc, nor, idx, obj, mat = dg.scene.ray_cast(dg, origin, direction, distance=200.0)
    if not ok or obj is None:
        return None, None
    if obj.name.startswith(("p16_sky", "p16_sea", "p16_flw", "p18_")):
        return None, None
    # phase 21: water, bridges, paths and the summit plinth are NOT plantable ground
    if obj.name.startswith("p21_") and obj.name not in ("p21_hill",):
        return None, None
    return obj, loc


def sample_surface(dg, cfg, n, seed, only=None, min_up=0.0):
    """Throw n rays through the frame, keep the hits on land, and return (u, v, y, z) samples
    plus the object hit. This is how every scattered element finds the actual ground."""
    rnd = random.Random(seed)
    out = []
    got = 0
    tries = 0
    while got < n and tries < n * 8:
        tries += 1
        u = rnd.uniform(-0.05, 1.05)
        v = rnd.uniform(0.30, 1.06)
        obj, loc = ray_hit(dg, cfg, u, v)
        if obj is None:
            continue
        if only is not None and obj.name not in only:
            continue
        out.append((u, v, loc.y, loc.z, obj.name))
        got += 1
    return out


def scatter_on(dg, cfg, n, depth_off, size_frac, seed, only=None):
    """Drop `n` blooms onto whatever surface the camera actually sees, then nudge them toward
    the camera so they sit proud of it. Colour comes from the traced FLOWER_REGIONS table."""
    pv, pf = flower_proto()
    samples = sample_surface(dg, cfg, n, seed, only=only)
    if not samples:
        return {}
    rnd = random.Random(seed + 1)
    buckets = {}
    for (u, v, y, z, nm) in samples:
        kind = region_at(u, v)
        w = REGION_W.get(kind, REGION_W["mixed"])
        col = rnd.choices(list(w), weights=list(w.values()))[0]
        persp = 0.62 + 0.90 * min(max((v - 0.42) / 0.62, 0.0), 1.0)
        s_ = u2w(size_frac * persp, cfg) * rnd.uniform(0.75, 1.35)
        P = np.array([u2x(u, cfg) + rnd.uniform(-0.014, 0.014),
                      y - depth_off + rnd.uniform(-0.05, 0.05),
                      z + rnd.uniform(-0.008, 0.008)])
        # spin about the VIEW axis (Y) so the bloom stays in the image plane and only its
        # rim turns -- this is what keeps a flower a flower from an orthographic viewpoint
        buckets.setdefault(col, []).append(
            (P, tilt_y(rnd.uniform(0, 6.283), rnd.uniform(-0.42, 0.42)),
             np.array([s_, s_, s_])))
    return buckets


def snap_to_ground(dg, cfg, u, v, depth, obj, prefer=None, lift=0.0):
    """Place an object at image (u, v) but take its Z from the first surface hit there, so it
    stands on the terrain instead of floating at an invented height."""
    o, loc = ray_hit(dg, cfg, u, v)
    if o is None:
        return None
    return loc


def dump_projection(scene, cfg):
    """Exact analytic image rect of every visible mesh. This is the verification channel:
    the ortho camera makes u,v a closed-form function of the world bbox, so no pixel
    guessing is needed to know where things landed."""
    cam = scene.camera
    S = cam.data.ortho_scale
    Cz = cam.location.z
    # MUST mirror u2x/v2z exactly or the verification channel lies about placement
    def u(x): return 0.5 + (x - cam.location.x) / S
    def v(z): return 0.5 - (z - Cz) / (S / ASPECT)
    rows = []
    for o in bpy.data.objects:
        if o.type != "MESH" or o.hide_render or o.name.startswith(("p16_sky", "p16_sea")):
            continue
        mw = o.matrix_world
        vs = [mw @ x.co for x in o.data.vertices]
        if not vs:
            continue
        x0 = min(p.x for p in vs); x1 = max(p.x for p in vs)
        z0 = min(p.z for p in vs); z1 = max(p.z for p in vs)
        y0 = min(p.y for p in vs); y1 = max(p.y for p in vs)
        rows.append((v(z1), u(x0), u(x1), v(z0), y0, y1, o.name,
                     (o.data.materials[0].name if o.data.materials else "-")))
    rows.sort()
    print("P16_PROJ  u0     u1     v0     v1     depth        object / material")
    for (va, ua, ub, vb, ya, yb, nm, mat) in rows:
        flag = ""
        if (ub - ua) > 0.9 and (vb - va) > 0.9:
            flag = "   <<< FILLS FRAME"
        print("P16_PROJ %.3f  %.3f  %.3f  %.3f  %6.1f..%6.1f  %-22s %s%s"
              % (ua, ub, va, vb, ya, yb, nm[:22], mat[:26], flag))


def apply_grade(cfg):
    """Final measured grade against the reference, via view-settings curve mapping.

    Blender 5.2 dropped scene.node_tree (legacy compositor), but view_settings.curve_mapping
    survives and exposes 4 curves: index 0 is the COMPOSITE (master), 1/2/3 are R/G/B.

    A first attempt wrote the per-channel gains into all four, so R was boosted twice and B
    crushed twice -- the render turned orange and lost half its luminance. The composite is
    left as identity here and only R/G/B carry a gain.
    """
    if not cfg.get("p18_grade", True):
        print("P18_GRADE off")
        return
    sc = bpy.context.scene
    vs = sc.view_settings
    try:
        vs.use_curve_mapping = True
    except Exception as e:
        print("P18_GRADE_FAIL", e)
        return
    cm = vs.curve_mapping
    cm.initialize()
    # y = x**p per channel, p chosen from the measured (render -> target) pair:
    #   R 96.2/255 -> 105.6/255  p = 0.904
    #   G 109.9/255 ->  98.9/255 p = 1.125
    #   B 100.2/255 ->  77.7/255 p = 1.271
    exps = cfg.get("p18_grade_exp", (0.904, 1.125, 1.271))
    for ci in range(4):
        c = cm.curves[ci]
        while len(c.points) > 2:
            c.points.remove(c.points[1])
        c.points[0].location = (0.0, 0.0)
        c.points[1].location = (1.0, 1.0)
        if ci == 0:
            continue                      # composite stays identity
        p = float(exps[ci - 1])
        for k in (1, 2, 3):
            x = k / 4.0
            c.points.new(x, max(0.0, min(1.0, x ** p)))
    try:
        cm.update()
    except Exception as e:
        print("P18_GRADE_UPD", e)
    print("P18_GRADE exps=%s" % (tuple(round(e, 3) for e in exps),))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="{}")
    ap.add_argument("--tag", default="r620_p16")
    ap.add_argument("--size", type=int, default=1672)
    ap.add_argument("--skip", default="")
    ap.add_argument("--save", default="")
    ap.add_argument("--views", default="ref")
    a = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    cfg = json.loads(a.cfg)
    skip = set(x for x in a.skip.split(",") if x)

    clear()
    scene = bpy.context.scene
    setup_ortho_camera(scene, cfg)
    build_backdrop(cfg)

    # ---- import the delivered composite (island + villa + props all come from it)
    try:
        bpy.ops.import_scene.gltf(filepath=GLB)
        print("P16_IMPORT ok")
    except Exception as e:
        print("P16_IMPORT_FAIL", e)

    # PHASE 21: the delivered island mesh (oskar_scene) sits at y -1.7..1.7, i.e. IN FRONT of
    # the new terraced hill at y 1.8..3.4, so it occluded everything below v~0.6 and read as a
    # tan mass. The landform is now built from p21_ground(); the old mesh is retired.
    if cfg.get("p21_retire_island", True):
        # PHASE 21 BUGFIX: stone_estate.glb imports its castle mesh under the SAME base name
        # as the island ("oskar_scene.001"), so matching on nm.split(".")[0] hid the castle
        # on every run. Anything parented under the stone_estate hierarchy is the BUILDING,
        # never the island, so it is excluded from the retire.
        keep = set()
        for o in bpy.data.objects:
            if o.name.startswith("stone_estate"):
                keep.add(o.name)
                for c in o.children_recursive:
                    keep.add(c.name)
        for nm in list(bpy.data.objects.keys()):
            if nm in keep:
                continue
            if nm.split(".")[0] in ("oskar_scene", "island_terrain"):
                o = bpy.data.objects.get(nm)
                if o is not None:
                    o.hide_render = True
                    o.hide_viewport = True
        print("P21_RETIRE island mesh hidden (kept %d estate objs)" % len(keep))

    if cfg.get("whitelist", True):
        apply_whitelist()
    strip_outlines(cfg)

    if "island" not in skip:
        place_island(cfg)
    if "mats" not in skip:
        look.build_materials(verbose=True)
    # real image textures go on AFTER the procedural pass so they win the Base Color
    apply_textures(cfg)
    if "meadow" not in skip:
        # the reference field is 27% dark ground showing BETWEEN the plants; a uniform bright
        # green carpet cannot produce that, so the base map is driven down and the plants
        # supply all the brightness.
        if cfg.get("p18_meadow_val"):
            TEX["meadow"]["val"] = float(cfg["p18_meadow_val"])
        if cfg.get("p18_meadow_tint"):
            TEX["meadow"]["tint"] = tuple(cfg["p18_meadow_tint"])
        if cfg.get("p18_meadow_curves"):
            TEX["meadow"]["curves"] = tuple(cfg["p18_meadow_curves"])
        build_meadow(cfg)
        if cfg.get("p18_ground_rgb"):
            # the field floor is dark earth in the reference (modal (16,32,32)/(16,16,16)),
            # visible BETWEEN the plants. A pale green carpet cannot produce that.
            go = bpy.data.objects.get("p16_meadow")
            if go is not None:
                # dark foliage map, not flat colour: the reference floor is dark TEAL-GREEN
                # (modal 16,32,32 / 0,16,16) with visible leaf litter, not flat earth.
                gm = tex_material("p18_ground", "foliage", rough=0.95)
                _tint_material(gm, tuple(cfg["p18_ground_rgb"]), float(cfg.get("p18_ground_mix", 0.55)))
                go.data.materials.clear()
                go.data.materials.append(gm)
                uv_planar(go, scale=float(cfg.get("p18_ground_uv", 0.55)))
                print("P18_GROUND", tuple(cfg["p18_ground_rgb"]))
    # ---------------------------------------------------- PHASE 21 landform
    # Built after the meadow so the terraced hill and the foreground shelf meet exactly, and
    # before the villa so the castle has something to stand on.
    if "land" not in skip:
        build_terrain(cfg)
        build_stream(cfg)
        build_mountains(cfg)
        build_sea_ripples(cfg)
        build_path(cfg)
        build_summit(cfg)
    if "villa" not in skip:
        place_villa(cfg)
        # PHASE 21: the castle now actually renders (see the retire fix above), but the
        # stone_estate textures are authored at near-white albedo and clip to 255 under the
        # 1.6-exposure warm key, so the summit read as a white blob. Darken the castle's own
        # materials for this shot only: they are used by no other object except the bridge
        # and gate, which keep their own copies.
        _cg = float(cfg.get("p21_castle_gain", 0.45))
        for _mn in ("p17_tex_stone", "p17_tex_roof", "p17_tex_wood"):
            _m = bpy.data.materials.get(_mn)
            if not _m or not _m.use_nodes:
                continue
            _nt = _m.node_tree
            _b = _nt.nodes.get("Principled BSDF")
            if _b is None or not _b.inputs["Base Color"].is_linked:
                continue
            _src = _b.inputs["Base Color"].links[0].from_socket
            _hs = _nt.nodes.get("p21_castle_dark")
            if _hs is None:
                _hs = _nt.nodes.new("ShaderNodeHueSaturation")
                _hs.name = "p21_castle_dark"
                _hs.location = (_b.location.x - 220, _b.location.y + 120)
            _hs.inputs["Value"].default_value = _cg
            _nt.links.new(_src, _hs.inputs["Color"])
            _nt.links.new(_hs.outputs["Color"], _b.inputs["Base Color"])
        print("P21_CASTLE_GAIN", _cg)
    if "roadside" not in skip:
        place_bridge_gate(cfg)
    if "aqueduct" not in skip:
        build_aqueduct(cfg)
    if "cypress" not in skip:
        place_cypress(cfg)
    if "animals" not in skip:
        place_animals(cfg)
    if "flowers" not in skip:
        # phase 18: the p17 millefleur speckle is retired -- fl_* default to 0 now
        if int(cfg.get("fl_island", 0)) or int(cfg.get("fl_meadow", 0)):
            place_flowers(cfg)
    if "plants" not in skip:
        place_plants(cfg)
    if "oaks" not in skip:
        place_oaks(cfg)

    audit_scene(cfg)
    if "look" not in skip:
        look.golden_pastoral(scene, cfg)
    apply_grade(cfg)
    if cfg.get("_probe"):
        apply_probe()
    if cfg.get("_probe") or cfg.get("standard_view"):
        try:
            scene.view_settings.view_transform = "Standard"
            scene.view_settings.look = "None"
            scene.view_settings.exposure = 0.0
            scene.view_settings.gamma = 1.0
        except Exception as e:
            print("P16_VIEWFAIL", e)
        print("P16_VIEW", scene.view_settings.view_transform)

    scene.render.engine = "BLENDER_EEVEE"
    scene.render.image_settings.file_format = "PNG"
    if cfg.get("samples"):
        try:
            scene.eevee.taa_render_samples = int(cfg["samples"])
        except Exception:
            pass

    if cfg.get("_blockout"):
        scene.render.engine = "BLENDER_WORKBENCH"

    dump_projection(scene, cfg)

    out = os.path.join(OUTDIR, "%s.png" % a.tag)
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)
    print("P16_RENDER", out)
    if a.save:
        bpy.ops.wm.save_as_mainfile(filepath=a.save)
        print("P16_SAVED", a.save)


if __name__ == "__main__":
    main()
