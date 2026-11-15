#!/usr/bin/env python3
"""Phase 15 — material & lighting overhaul for the Maurice Island scene.

Standalone Blender script:
    Blender -b -P scripts/p15_materials.py -- [--cfg '{"flowers":1,"haze":1}'] [--out r5xx] [--size 1200]

Rebuilds every material procedurally (no image files), replaces the flat studio
light with a "golden pastoral" rig, adds the flower mass / framing branches /
grounded horses+rabbits, and renders the reference camera plus two extra views.
"""
import bpy, bmesh, sys, os, math, json, random, argparse
import numpy as np
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree

P = "/Users/panglaohu/Downloads/GenAIWorkPipeline"
RUN = P + "/runs/20261026-202458-9ac6"
GLB = RUN + "/blender/export/scene_composite.glb"
OUTDIR = RUN + "/blender/renders"
TARGET = RUN + "/references/morris_island_target.png"

# reference camera: the framing found in phase 14 to match the target composition
# ---- phase 16 camera: fitted against the reference's land-fraction profile.
# The target is a wide meadow headland: land fills y>=40%, the sea is a band above the crest
# ending at the horizon near y=21%, and the villa sits in the upper third at x~55%.
# camera solved from the reference geometry: horizon at 20% of frame height, island crest at
# ~37%, 32 deg lens -> eye 3.2 m up, 9 m out, pitched 5.5 deg down.
CAM_REF = {"eye": [0.0, -20.0, 4.00], "target": [0.0, -6.0, 2.386], "fov_deg": 38.0, "aspect": 16 / 9}
CAM_BLOCKOUT = {"eye": [0.0, -15.5, 4.15], "target": [0.05, 0.0, 3.05], "fov_deg": 30.0, "aspect": 16 / 9}
CAM_CHAMP = {"eye": [6.462, -12.609, 11.987], "target": [6.462, -0.278, 5.008], "fov_deg": 61.89, "aspect": 1.7768}

random.seed(1501); np.random.seed(1501)


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


def build_materials():
    """replace every material's flat colour with a procedural graph"""
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
    print("P15_MATERIALS", dict(sorted(counts.items(), key=lambda x: -x[1])))


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


# ============================================================ phase 16: traced layout
# The target is a *hillside promontory seen from across the water*, not an aerial map.
# Measured from references/morris_island_target.png (1672x941):
#   sea band   y 18%..50% on the left/right thirds, horizon ~= 18..20%
#   land       fills y 45%..100% (foreground flower slope)
#   villa      x 45..60%, y 28..50%  (tower slightly right of centre, upper third)
#   cypress    vertical accents flanking villa, x 38..66%
#   big oaks   top-left x 0..26% and top-right x 74..100%, canopy in y 0..35%
#   bridge     x 47..57%, y 62..72%
#   horses     x 20% y 65%, x 30% y 55%, x 85% y 65%
#   rabbit     x 15% y 88%
# All positions below are in *screen* percent and get unprojected onto the terrain.
LAYOUT = {
    "horizon_pct": 20.0,
    "villa":   {"x": 0.545, "y": 0.385, "width_pct": 0.170},
    "tower":   {"x": 0.583, "y": 0.325},
    "gate":    {"x": 0.500, "y": 0.585},
    "bridge":  {"x": 0.520, "y": 0.665},
    "horses":  [{"x": 0.205, "y": 0.655, "kind": "white"},
                {"x": 0.315, "y": 0.585, "kind": "brown"},
                {"x": 0.845, "y": 0.660, "kind": "white"}],
    "rabbits": [{"x": 0.150, "y": 0.880}, {"x": 0.680, "y": 0.815}],
    # cypresses clustered around the villa and along the ridge behind it
    "cypress_spread": {"x0": 0.36, "x1": 0.70, "y0": 0.245, "y1": 0.470, "n": 34},
}


def screen_to_ground(bvh, cam, sx, sy, minz=-1.0, maxz=None):
    """Unproject a normalised screen coord through the camera and drop it onto the terrain.
    Returns world XYZ (or None if the ray misses land)."""
    import bpy as _b
    from mathutils import Vector as V
    sc = _b.context.scene
    # camera basis
    E = V(cam["eye"]); T = V(cam["target"])
    f = (T - E).normalized()
    r = f.cross(V((0, 0, 1))).normalized()
    u = r.cross(f).normalized()
    aspect = cam.get("aspect", 16 / 9)
    hx = math.radians(cam["fov_deg"]) * 0.5
    ty = math.tan(hx) / aspect
    # sx,sy in 0..1 with y measured from the TOP of the frame
    dx = (sx * 2 - 1) * math.tan(hx)
    dy = (1 - sy * 2) * ty
    d = (f + r * dx + u * dy).normalized()
    hit, nrm, idx, dist = bvh.ray_cast(E, d, 400.0)
    if hit is None:
        return None
    return hit




def build_apron(cfg):
    """A meadow slope *in front of* the existing island, not around it.

    Solved from the reference: horizon at 20% of frame, island crest at ~37%, land filling
    everything below ~45%. With a 32 deg lens that means the camera sits ~3.2 m up and 9 m
    out, pitched 5.5 deg down. From there the island's own near face is a rock cliff, but the
    target's foreground is a flower slope rising to the villa -- so this adds that slope,
    running from just above the waterline up to the island plateau edge.

    The island mesh, villa, cypresses, path, bridge and layout are untouched.
    """
    if not cfg.get("apron", 1):
        return
    YN  = cfg.get("ap_near", -30.0)     # slope starts behind the camera
    YI  = cfg.get("ap_island", -1.62)   # where it meets the island rim
    ZN  = cfg.get("ap_z_near", 0.10)
    ZI  = cfg.get("ap_z_island", 2.70)
    RISE= cfg.get("ap_rise_pow", 1.55)   # most of the climb happens near the island
    HWN = cfg.get("ap_half_near", 11.0)
    HW  = cfg.get("ap_half_island", 2.30)
    NU  = int(cfg.get("apron_nu", 120))
    NV = int(cfg.get("apron_nv", 90))
    rnd = random.Random(11)

    def sstep(a_, b_, x):
        t = min(1.0, max(0.0, (x - a_) / (b_ - a_)))
        return t * t * (3 - 2 * t)

    def height(x, y):
        t = min(1.0, max(0.0, (y - YN) / (YI - YN)))
        # slope profile: gentle at the toe, steepening as it climbs to the plateau
        crest = ZN + (ZI - ZN) * (t ** RISE)
        hw = HWN + (HW - HWN) * t
        cross = 1.0 - sstep(0.68, 1.0, abs(x) / max(hw, 1e-6))
        h = -0.55 + (crest + 0.55) * cross
        h += 0.055 * math.sin(x * 1.9 + 0.4) * math.cos(y * 1.5 - 0.3) * max(0.0, cross)
        h += 0.028 * math.sin(x * 4.1 - 0.8) * math.cos(y * 3.2 + 0.7) * max(0.0, cross)
        return h

    V, F = [], []
    x0, x1 = -HWN * 1.55, HWN * 1.55
    y0, y1 = YN - 1.2, YI + 0.15
    for j in range(NV + 1):
        y = y0 + (j / NV) * (y1 - y0)
        for i in range(NU + 1):
            x = x0 + (i / NU) * (x1 - x0)
            V.append((x, y, height(x, y) + rnd.uniform(-0.006, 0.006)))
    W = NU + 1
    for j in range(NV):
        for i in range(NU):
            a0 = j * W + i; a1 = a0 + 1; b0 = a0 + W; b1 = b0 + 1
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    me = bpy.data.meshes.new("p15_apron")
    me.from_pydata(V, [], F)
    me.validate()
    ob = bpy.data.objects.new("p15_apron", me)
    bpy.context.collection.objects.link(ob)
    mat = bpy.data.materials.get("p15_apron_grass")
    if mat is None:
        mat = bpy.data.materials.new("p15_apron_grass")
        mat_meadow(mat)
    me.materials.append(mat)
    for p_ in me.polygons:
        p_.use_smooth = False
    print("P15_APRON slope verts=%d faces=%d y[%.1f,%.1f] z[%.2f,%.2f]"
          % (len(V), len(F), y0, y1, ZN, ZI))
    return ob




# ------------------------------------------------------------- scene props
def build_bvh():
    """combined terrain BVH: island first, then the phase-16 foreground apron"""
    names = ["oskar_scene"]
    if bpy.data.objects.get("p15_apron"):
        names.append("p15_apron")
    V, F = [], []
    for n in names:
        o = bpy.data.objects[n]
        mw = o.matrix_world
        off = len(V)
        V.extend([mw @ v.co for v in o.data.vertices])
        F.extend([tuple(i + off for i in p.vertices) for p in o.data.polygons])
    return BVHTree.FromPolygons(V, F, all_triangles=False)


def ground(bvh, x, y, minz=0.55):
    l, ln, li, ld = bvh.ray_cast(Vector((x, y, 40.0)), Vector((0, 0, -1)))
    if l is None or abs(ln.z) < 0.22 or l.z < minz:
        return None
    return l.z


def make_proto(src_name, head_ratio, tag):
    src = bpy.data.objects[src_name]
    me = src.data
    petal = {i for i, m in enumerate(me.materials) if m and m.name.startswith("osk_petal")}
    hf = [p for p in me.polygons if p.material_index in petal]
    sf = [p for p in me.polygons if p.material_index not in petal]

    def part(faces, nm):
        bm = bmesh.new()
        vm = {}
        for p in faces:
            vs = []
            for vi in p.vertices:
                if vi not in vm:
                    vm[vi] = bm.verts.new(me.vertices[vi].co)
                vs.append(vm[vi])
            try:
                bm.faces.new(vs)
            except Exception:
                pass
        m3 = bpy.data.meshes.new(nm)
        bm.to_mesh(m3)
        bm.free()
        ob = bpy.data.objects.new(nm, m3)
        bpy.context.collection.objects.link(ob)
        return ob

    ho = part(hf, tag + "_h")
    so = part(sf, tag + "_s")
    for ob, r in ((ho, head_ratio), (so, 0.14)):
        bpy.context.view_layer.objects.active = ob
        md = ob.modifiers.new("d", "DECIMATE")
        md.ratio = r
        bpy.ops.object.modifier_apply(modifier=md.name)
    allw = [[*(ob.matrix_world @ v.co)] for ob in (ho, so) for v in ob.data.vertices]
    arr = np.array(allw, float)
    mn, mx = arr.min(0), arr.max(0)
    ctr = np.array([(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2]])
    H = max(mx[2] - mn[2], 1e-6)
    out = []
    for ob in (ho, so):
        a = np.array([[*(ob.matrix_world @ v.co)] for v in ob.data.vertices], float)
        out.append(((a - ctr) / H, [tuple(p.vertices) for p in ob.data.polygons]))
    for ob in (ho, so):
        bpy.data.objects.remove(ob, do_unlink=True)
    return out[0], out[1]


HUE = {"red": (0.62, 0.055, 0.030), "scarlet": (0.79, 0.10, 0.035), "white": (0.96, 0.93, 0.85),
       "yellow": (0.93, 0.73, 0.10), "orange": (0.90, 0.40, 0.055), "rose": (0.78, 0.19, 0.20)}
WEIGHT = [0.30, 0.15, 0.17, 0.16, 0.16, 0.06]


def flower_mass(bvh, cfg):
    """thousands of small flowers ON the terrain, denser near the foreground slopes"""
    den = cfg.get("fl_den", 1.0)
    hscale = cfg.get("fl_h", 1.0)
    PA = make_proto("oskar_props.041", 0.26, "p15a")
    PB = make_proto("oskar_props.030", 0.34, "p15b")
    step = 0.042 / max(0.4, den)
    pts = []
    x = -2.7
    while x < 3.1:
        y = -2.5
        while y < 2.6:
            px = x + random.uniform(-step * 0.42, step * 0.42)
            py = y + random.uniform(-step * 0.42, step * 0.42)
            g = ground(bvh, px, py)
            if g is not None:
                keep = 0.80 if py < -0.2 else 0.66
                if random.random() < keep:
                    pts.append((px, py, g))
            y += step
        x += step

    buckets = {"A": {k: [] for k in HUE}, "B": {k: [] for k in HUE}}
    stA, stB = [], []
    for (x, y, z) in pts:
        typ = "A" if random.random() < 0.70 else "B"
        k = random.choices(list(HUE), weights=WEIGHT)[0]
        near = max(0.0, (0.9 - y) / 3.0)
        h = (0.058 + 0.032 * random.random()) * hscale * (1.0 + 0.30 * near if z < 1.8 else 0.92)
        if typ == "B":
            h *= 1.20
        R = rz(random.uniform(0, 6.283)) @ tilt(random.uniform(-0.07, 0.07), random.uniform(-0.07, 0.07))
        Pp = np.array([x, y, z - 0.006])
        buckets[typ][k].append((Pp, R, h))
        (stA if typ == "A" else stB).append((Pp, R, h))

    def m(name, rgb):
        mm = bpy.data.materials.new(name)
        mat_petal(mm, tuple(srgb2lin(c) for c in rgb))
        return mm

    for typ in ("A", "B"):
        hv, hfc = (PA[0] if typ == "A" else PB[0])
        for k, rgb in HUE.items():
            if buckets[typ][k]:
                merge(hv, hfc, buckets[typ][k], "p15_flw%s_%s" % (typ, k), m("p15_mat%s_%s" % (typ, k), rgb))
    sa = merge(PA[1][0], PA[1][1], stA, "p15_stemA", m("p15_mstemA", (0.16, 0.30, 0.11)))
    sb = merge(PB[1][0], PB[1][1], stB, "p15_stemB", m("p15_mstemB", (0.18, 0.32, 0.12)))
    sd = float(cfg.get("stem_decimate", 0.18))
    for ob in (sa, sb):
        if sd < 1.0:
            bpy.context.view_layer.objects.active = ob
            md = ob.modifiers.new("dec", "DECIMATE")
            md.ratio = sd
            bpy.ops.object.modifier_apply(modifier=md.name)
    print("P15_FLOWERS", len(pts))


def undergrowth(bvh, cfg):
    """A millefleur carpet needs green mass BETWEEN the flowers; without it the terrain
    shader's brown rock shows and the flowers read as sprinkles on bare ground."""
    dens = cfg.get("ug_den", 1.0)
    if dens <= 0:
        return
    leaf_mats = [m for m in bpy.data.materials
                 if m.users and (m.name.startswith("osk_leaf") or m.name.startswith("osk_canopy"))]
    if not leaf_mats:
        return
    # low-poly clump prototype
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=1, radius=1.0)
    for v in bm.verts:
        v.co.x *= 1.25
        v.co.z *= 0.55
    me = bpy.data.meshes.new("p15_ug_proto")
    bm.to_mesh(me)
    bm.free()
    pv = np.array([[v.co.x, v.co.y, v.co.z] for v in me.vertices], float)
    pf = [tuple(pp.vertices) for pp in me.polygons]
    bpy.data.objects.new("p15_ug_tmp", me)
    step = cfg.get("ug_step", 0.085) / max(0.3, dens)
    buckets = {i: [] for i in range(len(leaf_mats))}
    n = 0
    x = -2.7
    while x < 3.1:
        y = -2.6
        while y < 2.6:
            px = x + random.uniform(-step * 0.45, step * 0.45)
            py = y + random.uniform(-step * 0.45, step * 0.45)
            g = ground(bvh, px, py)
            if g is not None and random.random() < 0.80:
                sz = random.uniform(0.030, 0.070) * cfg.get("ug_size", 1.0)
                R = rz(random.uniform(0, 6.283))
                buckets[random.randrange(len(leaf_mats))].append((np.array([px, py, g - 0.012]), R, np.array([sz, sz, sz])))
                n += 1
            y += step
        x += step
    for i, tf in buckets.items():
        if tf:
            merge(pv, pf, tf, "p15_ug_%d" % i, leaf_mats[i], smooth=False)
    print("P15_UNDERGROWTH", n)


def cypress_cluster(bvh, cfg):
    """Real cypresses: the asset previously instanced (oskar_scene.012) is a flower prop,
    so 47 pale cylinders were being scattered as 'trees'. Build a tapered, noise-broken
    cone with a dark needle material instead."""
    dark = bpy.data.materials.get("p15_cypress_dark")
    if dark is None:
        dark = bpy.data.materials.new("p15_cypress_dark")
        mat_leaf(dark, dark=True)
        try:
            nt = dark.node_tree
            b = nt.nodes.get("Principled BSDF")
            if b:
                b.inputs["Base Color"].default_value = (*srgb2lin_c((0.055, 0.105, 0.048)), 1)
        except Exception:
            pass

    def make_cone(rings=9, seg=10):
        """tapered cypress: ring radius follows a fat-bottom/point-top profile with jitter"""
        V, F = [], []
        for i in range(rings):
            t = i / (rings - 1)                      # 0 bottom -> 1 top
            r = (1.0 - t) ** 0.78 * 0.30 + 0.012
            z = t
            for k in range(seg):
                a = 2 * math.pi * k / seg + i * 0.35
                jr = r * (1.0 + random.uniform(-0.22, 0.22))
                V.append((math.cos(a) * jr, math.sin(a) * jr, z + random.uniform(-0.012, 0.012)))
        for i in range(rings - 1):
            for k in range(seg):
                a0 = i * seg + k; a1 = i * seg + (k + 1) % seg
                b0 = (i + 1) * seg + k; b1 = (i + 1) * seg + (k + 1) % seg
                F.append((a0, a1, b1)); F.append((a0, b1, b0))
        return np.array(V, float), F

    pv, pf = make_cone()
    spots = []
    for i in range(60):
        spots.append((random.uniform(-1.9, 2.1), random.uniform(-1.7, 1.6), 1.0))
    vx, vy = cfg.get("villa_xy", (0.42, 0.62))
    for i in range(26):
        spots.append((vx + random.uniform(-0.85, 0.90), vy + random.uniform(-0.70, 0.95), 1.0))
    tf = []
    for (x, y, _) in spots:
        g = ground(bvh, x, y, minz=float(cfg.get("cyp_minz", 1.35)))
        if g is None:
            continue
        h = random.uniform(0.62, 1.30) * cfg.get("cyp_h", 1.0)
        w = random.uniform(0.55, 0.95) * cfg.get("cyp_w", 1.0)
        tf.append((np.array([x, y, g - 0.02]), rz(random.uniform(0, 6.283)), np.array([w, w, h])))
    if tf:
        merge(pv, pf, tf, "p15_cypress", dark)
    print("P15_CYPRESS", len(tf))


def ground_animals(bvh, cfg):
    HS = [bpy.data.objects[n] for n in ("saddled_horse", "saddled_horse.001", "saddled_horse.002")]
    hspots = cfg.get("hspots", [[0.05, -0.72], [1.05, -1.25], [1.75, -0.35]])
    hs = cfg.get("hscale", 0.50)
    for o, (x, y) in zip(HS, hspots):
        g = ground(bvh, x, y, minz=2.2)
        if g is None:                      # never float an animal over the sea
            print("P15_HORSE_SKIP no ground at", x, y)
            continue
        world = o.matrix_world.copy()
        wv = [world @ v.co for v in o.data.vertices]
        mnz = min(v.z for v in wv)
        cx = sum(v.x for v in wv) / len(wv)
        cy = sum(v.y for v in wv) / len(wv)
        o.matrix_world = (Matrix.Translation(Vector((x, y, g + 0.02)))
                          @ Matrix.Rotation(random.uniform(0, 6.283), 4, "Z")
                          @ Matrix.Diagonal((hs, hs, hs, 1.0))
                          @ Matrix.Translation(Vector((-cx, -cy, -mnz))) @ world)
    # red saddle blanket
    rm = bpy.data.materials.new("p15_saddle_red")
    mat_petal(rm, tuple(srgb2lin(c) for c in (0.78, 0.10, 0.075)))
    for o in HS:
        wv = [o.matrix_world @ v.co for v in o.data.vertices]
        mnx, mxx = min(v.x for v in wv), max(v.x for v in wv)
        mny, mxy = min(v.y for v in wv), max(v.y for v in wv)
        mnz, mxz = min(v.z for v in wv), max(v.z for v in wv)
        w, d, h = mxx - mnx, mxy - mny, mxz - mnz
        bpy.ops.mesh.primitive_cube_add(size=1, location=(mnx + w * 0.50, mny + d * 0.52, mnz + h * 0.60))
        bl = bpy.context.active_object
        bl.name = "p15_saddle_red"
        bl.scale = (w * 0.17, d * 0.40, h * 0.11)
        bl.data.materials.append(rm)
        for p in bl.data.polygons:
            p.use_smooth = False
    # rabbits: parts of one rabbit share the same name suffix ('' == rabbit 0)
    PARTS = {"body", "head", "haunch", "chest", "belly_patch", "snout", "nose", "tail",
             "ear_near", "ear_far", "ear_inner_near", "ear_inner_far", "eye_near", "eye_far",
             "front_leg_near", "front_leg_far", "front_paw_near", "front_paw_far",
             "hind_foot_near", "hind_foot_far"}
    groups = {}
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name.startswith("p15_"):
            continue
        parts = o.name.split(".")
        if parts[0] not in PARTS:
            continue
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        groups.setdefault(idx, []).append(o)
    rs = cfg.get("rscale", 2.6)
    rspots = cfg.get("rspots", [[-1.20, -1.35], [-0.55, -1.05]])
    placed = 0
    for i, sp in enumerate(rspots):
        if i not in groups:
            continue
        g = ground(bvh, sp[0], sp[1], minz=1.2)
        if g is None:
            g = ground(bvh, sp[0], sp[1], minz=0.6)
        if g is None:
            continue
        grp = groups[i]
        # group pivot
        allv = [o.matrix_world @ v.co for o in grp for v in o.data.vertices]
        gx = sum(v.x for v in allv) / len(allv)
        gy = sum(v.y for v in allv) / len(allv)
        gz = min(v.z for v in allv)
        R = Matrix.Rotation(random.uniform(0, 6.283), 4, "Z")
        M = (Matrix.Translation(Vector((sp[0], sp[1], g + 0.01)))
             @ R @ Matrix.Diagonal((rs, rs, rs, 1.0))
             @ Matrix.Translation(Vector((-gx, -gy, -gz))))
        for o in grp:
            o.matrix_world = M @ o.matrix_world
        placed += 1
        print("P15_ANIMALS horses=%d rabbits=%d" % (len(HS), placed))


def framing_branches(cfg, cam):
    E = Vector(cam["eye"]); T = Vector(cam["target"])
    f = (T - E).normalized()
    r = f.cross(Vector((0, 0, 1))).normalized()
    u = r.cross(f).normalized()
    fov = math.radians(cam["fov_deg"]); aspect = cam.get("aspect", 16 / 9)
    thx = math.tan(fov / 2); thy = thx / aspect

    def mkm(name, rgb, rough=0.9):
        mm = bpy.data.materials.new(name)
        if name.endswith("bark"):
            mat_wood(mm)
        else:
            mat_leaf(mm)
        return mm

    BARK = mkm("p15_bark", (0.30, 0.20, 0.11))
    LEAF = mkm("p15_branchleaf", (0.20, 0.34, 0.11))
    FRUIT = bpy.data.materials.new("p15_fruit")
    mat_petal(FRUIT, tuple(srgb2lin(c) for c in (0.92, 0.46, 0.06)))

    def cam_pt(d, sx, sy):
        return E + f * d + r * (sx * thx * d) + u * (sy * thy * d)

    def tube(pts, radii, name, material, seg=8):
        rings = []
        for i, p in enumerate(pts):
            tan = (pts[i + 1] - p) if i < len(pts) - 1 else (p - pts[i - 1])
            tan.normalize()
            a = Vector((0, 0, 1)).cross(tan)
            if a.length < 1e-4:
                a = Vector((1, 0, 0))
            a.normalize(); b = tan.cross(a).normalized()
            rings.append([p + a * (math.cos(2 * math.pi * k / seg) * radii[i])
                          + b * (math.sin(2 * math.pi * k / seg) * radii[i]) for k in range(seg)])
        V = [v for ring in rings for v in ring]
        F = []
        for ri in range(len(rings) - 1):
            for k in range(seg):
                a0 = ri * seg + k; a1 = ri * seg + (k + 1) % seg
                b0 = (ri + 1) * seg + k; b1 = (ri + 1) * seg + (k + 1) % seg
                F.append((a0, a1, b1)); F.append((a0, b1, b0))
        me2 = bpy.data.meshes.new(name)
        me2.from_pydata([tuple(v) for v in V], [], F)
        me2.validate(); me2.materials.append(material)
        ob = bpy.data.objects.new(name, me2)
        bpy.context.collection.objects.link(ob)
        for p in me2.polygons:
            p.use_smooth = True
        return ob

    def ico(center, radius, name, material, subdiv=2, squash=0.75):
        bm = bmesh.new()
        bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=radius)
        for v in bm.verts:
            v.co.z *= squash
        me3 = bpy.data.meshes.new(name)
        bm.to_mesh(me3); bm.free(); me3.materials.append(material)
        ob = bpy.data.objects.new(name, me3)
        bpy.context.collection.objects.link(ob)
        ob.location = center
        for p in me3.polygons:
            p.use_smooth = False
        return ob

    DIST = cfg.get("branch_dist", 2.20)
    SX = cfg.get("branch_sx", 1.32); SY = cfg.get("branch_sy", 0.97)
    MX = cfg.get("branch_mx", 0.62); MY = cfg.get("branch_my", 0.52)
    TX = cfg.get("branch_tx", 0.52); TY = cfg.get("branch_ty", 0.58)
    NLEAF = cfg.get("branch_leaves", 10); NFRUIT = cfg.get("branch_fruit", 8)
    for side, sgn in (("L", -1.0), ("R", 1.0)):
        base = cam_pt(DIST, sgn * SX, SY)
        mid = cam_pt(DIST, sgn * MX, MY)
        tip = cam_pt(DIST, sgn * TX, TY)
        pts = [base, (base + mid) / 2, mid, (mid + tip) / 2, tip]
        br = cfg.get("branch_r", 0.032)
        tube(pts, [br, br * 0.82, br * 0.58, br * 0.38, br * 0.20], "p15_branch_" + side, BARK, 8)
        for j, (t, off, rr) in enumerate([(0.35, 0.16, 0.030), (0.62, -0.13, 0.024)]):
            p0 = pts[2] + (pts[3] - pts[2]) * t
            p1 = p0 + r * (sgn * off * 0.7) + u * (-0.10) + f * 0.02
            tube([p0, (p0 + p1) / 2, p1], [rr, rr * 0.7, rr * 0.4], "p15_twig_%s%d" % (side, j), BARK, 6)
        for k in range(NLEAF):
            t = 0.30 + 0.70 * (k / max(1, NLEAF - 1))
            pp = pts[2] + (pts[4] - pts[2]) * t
            jit = (r * (sgn * random.uniform(-0.10, 0.10)) + u * random.uniform(-0.10, 0.09)
                   + f * random.uniform(-0.05, 0.05))
            ico(pp + jit, random.uniform(0.055, 0.105), "p15_leaf_%s%d" % (side, k), LEAF, 1, 0.55)
        for k in range(NFRUIT):
            t = 0.32 + 0.62 * (k / max(1, NFRUIT - 1))
            pp = pts[2] + (pts[4] - pts[2]) * t
            hang = u * (-random.uniform(0.10, 0.20)) + r * (sgn * random.uniform(-0.05, 0.05))
            ico(pp + hang, random.uniform(0.030, 0.045), "p15_fruit_%s%d" % (side, k), FRUIT, 1, 1.0)
    print("P15_BRANCHES ok")


def fit_villa(bvh, cfg):
    """The villa asset was composed 1.7x wider than the whole island (4.28x6.98 m on a
    4.04x3.40 m island), so any camera close enough to fill the frame became a wall of
    building. The target's villa is ~37% of island width; rescale and seat it on the peak."""
    sc = cfg.get("villa_scale", 0.45)
    if sc <= 0:
        return
    v = bpy.data.objects["oskar_scene.001"]
    w0 = v.matrix_world.copy()
    wv = [w0 @ x.co for x in v.data.vertices]
    mnx = min(a.x for a in wv); mxx = max(a.x for a in wv)
    mny = min(a.y for a in wv); mxy = max(a.y for a in wv)
    mnz = min(a.z for a in wv)
    cx, cy = (mnx + mxx) / 2, (mny + mxy) / 2
    tx, ty = cfg.get("villa_xy", (0.42, 0.62))
    g = ground(bvh, tx, ty, minz=2.0) or 2.70
    v.matrix_world = (Matrix.Translation(Vector((tx, ty, g - 0.02)))
                      @ Matrix.Rotation(math.radians(cfg.get("villa_rot", 8.0)), 4, "Z")
                      @ Matrix.Diagonal((sc, sc, sc, 1.0))
                      @ Matrix.Translation(Vector((-cx, -cy, -mnz))) @ w0)
    nv = [v.matrix_world @ x.co for x in v.data.vertices]
    print("P15_VILLA scale=%.2f w=%.2f->%.2f z=%.2f->%.2f at(%.2f,%.2f,%.2f)" % (
        sc, mxx - mnx, max(a.x for a in nv) - min(a.x for a in nv),
        mnz, min(a.z for a in nv), tx, ty, g))


def seat_roadside(bvh, cfg):
    """the stone gate + bridge + tower props should sit on the island, not float"""
    moved = 0
    for name in ("stone_path_gate", "stone_arch_bridge", "stone_arch_bridge.001",
                 "stone_arch_bridge.002", "stone_arch_bridge.003"):
        o = bpy.data.objects.get(name)
        if not o:
            continue
        wv = [o.matrix_world @ x.co for x in o.data.vertices]
        if not wv:
            continue
        mnz = min(a.z for a in wv)
        cx = sum(a.x for a in wv) / len(wv)
        cy = sum(a.y for a in wv) / len(wv)
        g = ground(bvh, cx, cy, minz=0.4)
        if g is not None and abs(mnz - g) > 0.30:
            o.matrix_world = Matrix.Translation(Vector((0, 0, g - mnz))) @ o.matrix_world
            moved += 1
    print("P15_SEATED", moved)


# --------------------------------------------------- phase 16: traced assembly stages
def _cam_basis(cam):
    E = Vector(cam["eye"]); T = Vector(cam["target"])
    f = (T - E).normalized()
    r = f.cross(Vector((0, 0, 1))).normalized()
    u = r.cross(f).normalized()
    return E, f, r, u


def screen_to_ground(bvh, cam, sx, sy, maxdist=300.0):
    """Unproject a normalised screen point (y from top) onto the terrain."""
    E, f, r, u = _cam_basis(cam)
    hx = math.tan(math.radians(cam["fov_deg"]) * 0.5)
    ty = hx / cam.get("aspect", 16 / 9)
    d = Vector((0, 0, 0))
    d += f * 1.0
    d += r * ((sx * 2 - 1) * hx)
    d += u * (((0.5 - sy) * 2) * ty)
    d.normalize()
    hit = bvh.ray_cast(E, d, maxdist)
    return hit[0] if hit[0] is not None else None


def place_at(bvh, cam, obj, sx, sy, cfg, yaw=0.0, scale=None, sink=0.02):
    """Move an object so its base sits on the terrain under screen point (sx, sy)."""
    g = screen_to_ground(bvh, cam, sx, sy)
    if g is None:
        print("P15_PLACE_MISS", obj.name, round(sx, 3), round(sy, 3))
        return None
    mw = obj.matrix_world.copy()
    if scale is not None:
        sc0 = mw.to_scale()
        if abs(sc0.x - scale) > 1e-6:
            mw = Matrix.Diagonal((scale / sc0.x, scale / sc0.y, scale / sc0.z, 1.0)) @ mw
    wv = [mw @ v.co for v in obj.data.vertices]
    mnz = min(v.z for v in wv)
    cx = sum(v.x for v in wv) / len(wv)
    cy = sum(v.y for v in wv) / len(wv)
    obj.matrix_world = (Matrix.Translation(Vector((g.x, g.y, g.z - sink)))
                        @ Matrix.Rotation(yaw, 4, "Z")
                        @ Matrix.Translation(Vector((-cx, -cy, -mnz))) @ mw)
    return g


def assemble_villa(bvh, cam, cfg):
    """Villa + tower placed by traced screen position, sized to the target's apparent width."""
    v = bpy.data.objects.get("oskar_scene.001")
    if v is None:
        return
    sx, sy = LAYOUT["villa"]
    # apparent width in the target is ~17% of frame width; convert to world size at this depth
    g = screen_to_ground(bvh, cam, sx, sy)
    if g is None:
        g = Vector((0.42, 0.62, 2.70))
    E = Vector(cam["eye"])
    dist = (g - E).length
    hx = math.tan(math.radians(cam["fov_deg"]) * 0.5)
    want_w = cfg.get("villa_w_pct", 0.170) * 2.0 * hx * dist      # world width for 17% of frame
    wv = [v.matrix_world @ x.co for x in v.data.vertices]
    cur_w = max(a.x for a in wv) - min(a.x for a in wv)
    sc = want_w / max(cur_w, 1e-6)
    place_at(bvh, cam, v, sx, sy, cfg, yaw=math.radians(cfg.get("villa_rot", 8.0)), scale=sc)
    print("P15_VILLA_PLACED sx=%.3f sy=%.3f dist=%.2f width %.2f->%.2f (%.1f%% of frame)"
          % (sx, sy, dist, cur_w, want_w, want_w / (2 * hx * dist) * 100))


def assemble_cypress(bvh, cam, cfg, proto):
    """Cypress clusters at their traced screen positions, not scattered at random."""
    pv, pf, mat = proto
    tf = []
    total = 0
    for (cx, cy_, n, spread) in CYP_CLUSTERS:
        for i in range(n):
            sx = cx + random.uniform(-spread, spread)
            sy = cy_ + random.uniform(-spread * 0.55, spread * 0.55)
            g = screen_to_ground(bvh, cam, sx, sy)
            if g is None:
                continue
            h = random.uniform(0.85, 1.45) * cfg.get("cyp_h", 1.0)
            w = random.uniform(0.50, 0.82) * cfg.get("cyp_w", 1.0)
            tf.append((np.array([g.x, g.y, g.z - 0.03]), rz(random.uniform(0, 6.283)),
                       np.array([w, w, h])))
            total += 1
    if tf:
        merge(pv, pf, tf, "p16_cypress", mat)
    print("P15_CYPRESS_TRACED", total)


def assemble_horses(bvh, cam, cfg):
    names = (("horse_white", "saddled_horse", (0.93, 0.92, 0.88)),
             ("horse_brown", "saddled_horse.001", (0.42, 0.26, 0.15)),
             ("horse_far", "saddled_horse.002", (0.90, 0.88, 0.84)))
    placed = 0
    for key, nm, col in names:
        o = bpy.data.objects.get(nm)
        if o is None:
            continue
        sx, sy = LAYOUT[key]
        # apparent width in the target ~7% of frame
        g = screen_to_ground(bvh, cam, sx, sy)
        if g is None:
            print("P15_HORSE_MISS", key)
            continue
        E = Vector(cam["eye"]); dist = (g - E).length
        hx = math.tan(math.radians(cam["fov_deg"]) * 0.5)
        want_w = cfg.get("horse_w_pct", 0.068) * 2 * hx * dist
        wv = [o.matrix_world @ v.co for v in o.data.vertices]
        cur_w = max(a.x for a in wv) - min(a.x for a in wv)
        sc = want_w / max(cur_w, 1e-6)
        place_at(bvh, cam, o, sx, sy, cfg, yaw=math.radians(random.uniform(-40, 40)), scale=sc)
        # recolour so the three horses read as white / brown / grey like the target
        m = bpy.data.materials.get("p16_horse_" + key)
        if m is None:
            m = bpy.data.materials.new("p16_horse_" + key)
            mat_petal(m, tuple(srgb2lin(c) for c in col))
        o.data.materials.clear()
        o.data.materials.append(m)
        placed += 1
    print("P15_HORSES_TRACED", placed)


def assemble_rabbits(bvh, cam, cfg):
    PARTS = {"body", "head", "haunch", "chest", "belly_patch", "snout", "nose", "tail",
             "ear_near", "ear_far", "ear_inner_near", "ear_inner_far", "eye_near", "eye_far",
             "front_leg_near", "front_leg_far", "front_paw_near", "front_paw_far",
             "hind_foot_near", "hind_foot_far"}
    groups = {}
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name.startswith("p15_"):
            continue
        parts = o.name.split(".")
        if parts[0] not in PARTS:
            continue
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        groups.setdefault(idx, []).append(o)
    ok = 0
    for i, key in enumerate(("rabbit_a", "rabbit_b")):
        if i not in groups:
            continue
        sx, sy = LAYOUT[key]
        g = screen_to_ground(bvh, cam, sx, sy)
        if g is None:
            continue
        E = Vector(cam["eye"]); dist = (g - E).length
        hx = math.tan(math.radians(cam["fov_deg"]) * 0.5)
        want_w = cfg.get("rabbit_w_pct", 0.022) * 2 * hx * dist
        grp = groups[i]
        allv = [o.matrix_world @ v.co for o in grp for v in o.data.vertices]
        gx = sum(v.x for v in allv) / len(allv)
        gy = sum(v.y for v in allv) / len(allv)
        gz = min(v.z for v in allv)
        cur_w = max(v.x for v in allv) - min(v.x for v in allv)
        sc = want_w / max(cur_w, 1e-6)
        M = (Matrix.Translation(Vector((g.x, g.y, g.z - 0.01)))
             @ Matrix.Rotation(random.uniform(0, 6.283), 4, "Z")
             @ Matrix.Diagonal((sc, sc, sc, 1.0))
             @ Matrix.Translation(Vector((-gx, -gy, -gz))))
        for o in grp:
            o.matrix_world = M @ o.matrix_world
        ok += 1
    print("P15_RABBITS_TRACED", ok)


def assemble_props(bvh, cam, cfg):
    """gate + bridge onto their traced screen positions"""
    for key, names in (("gate", ("stone_path_gate",)),
                       ("bridge", ("stone_arch_bridge", "stone_arch_bridge.001",
                                   "stone_arch_bridge.002", "stone_arch_bridge.003"))):
        sx, sy = LAYOUT[key]
        for nm in names:
            o = bpy.data.objects.get(nm)
            if o is None:
                continue
            g = screen_to_ground(bvh, cam, sx, sy)
            if g is None:
                continue
            E = Vector(cam["eye"]); dist = (g - E).length
            hx = math.tan(math.radians(cam["fov_deg"]) * 0.5)
            want_w = cfg.get(key + "_w_pct", 0.075 if key == "gate" else 0.085) * 2 * hx * dist
            wv = [o.matrix_world @ v.co for v in o.data.vertices]
            cur_w = max(a.x for a in wv) - min(a.x for a in wv)
            sc = want_w / max(cur_w, 1e-6)
            place_at(bvh, cam, o, sx, sy, cfg, yaw=math.radians(random.uniform(-12, 12)), scale=sc)

def make_cypress_proto(cfg):
    """tapered, noise-broken cypress cone with a dark needle material (returns pv, pf, mat)"""
    rings, seg = 9, 10
    V, F = [], []
    for i in range(rings):
        t = i / (rings - 1)
        r = (1.0 - t) ** 0.78 * 0.30 + 0.012
        z = t
        for k in range(seg):
            a = 2 * math.pi * k / seg + i * 0.35
            jr = r * (1.0 + random.uniform(-0.22, 0.22))
            V.append((math.cos(a) * jr, math.sin(a) * jr, z + random.uniform(-0.012, 0.012)))
    for i in range(rings - 1):
        for k in range(seg):
            a0 = i * seg + k; a1 = i * seg + (k + 1) % seg
            b0 = (i + 1) * seg + k; b1 = (i + 1) * seg + (k + 1) % seg
            F.append((a0, a1, b1)); F.append((a0, b1, b0))
    dark = bpy.data.materials.get("p15_cypress_dark")
    if dark is None:
        dark = bpy.data.materials.new("p15_cypress_dark")
        mat_leaf(dark, dark=True)
        try:
            b = dark.node_tree.nodes.get("Principled BSDF")
            if b:
                b.inputs["Base Color"].default_value = (*srgb2lin_c((0.055, 0.105, 0.048)), 1)
        except Exception:
            pass
    return np.array(V, float), F, dark


# flower colour recipes per traced region (dominant bloom colour in the target)
REGION_HUE = {
    "white":  {"white": 0.62, "rose": 0.16, "red": 0.10, "yellow": 0.07, "orange": 0.05},
    "red":    {"red": 0.52, "scarlet": 0.24, "orange": 0.12, "white": 0.08, "rose": 0.04},
    "scarlet": {"scarlet": 0.46, "red": 0.28, "orange": 0.16, "white": 0.07, "rose": 0.03},
    "orange": {"orange": 0.48, "yellow": 0.22, "red": 0.18, "white": 0.09, "rose": 0.03},
    "mixed":  {"red": 0.26, "white": 0.24, "orange": 0.18, "scarlet": 0.14, "yellow": 0.12, "rose": 0.06},
}


def _region_of(x, y):
    """pick the traced flower region that covers screen point (x,y); nearest centre wins"""
    best, bd = "mixed", 1e9
    for (cx, cy, rad, kind) in FLOWER_REGIONS:
        d = math.hypot((x - cx) / max(rad, 1e-6), (y - cy) / max(rad * 0.75, 1e-6))
        if d < bd:
            bd, best = d, kind
    return best


def flower_mass_traced(bvh, cam, cfg):
    """Millefleur carpet, scattered in SCREEN space so flower density and dominant colour
    match the reference region by region (white daisies left, red tulips right, mixed
    foreground). Each flower is unprojected onto the terrain, so they sit on the ground."""
    dens = float(cfg.get("fl_den", 1.0))
    if dens <= 0:
        return
    hs = float(cfg.get("fl_h", 1.0))
    A = make_proto("oskar_props.041", 0.26, "p16a")
    B = make_proto("oskar_props.030", 0.34, "p16b")
    # sample a grid over the *lower* part of the frame in screen space
    n = int(15000 * dens)
    rnd = random.Random(2027)
    buckets = {"A": {}, "B": {}}
    stems = []
    made = 0
    tries = 0
    while made < n and tries < n * 6:
        tries += 1
        sx = rnd.uniform(0.0, 1.0)
        sy = rnd.uniform(0.30, 1.0)
        g = screen_to_ground(bvh, cam, sx, sy)
        if g is None:
            continue
        # keep flowers off the villa / far horizon
        if sy < 0.33:
            continue
        kind = _region_of(sx, sy)
        w = REGION_HUE.get(kind, REGION_HUE["mixed"])
        typ = "A" if rnd.random() < 0.68 else "B"
        col = rnd.choices(list(w), weights=list(w.values()))[0]
        # perspective: flowers farther up the frame are farther away, so scale down
        persp = 0.42 + 0.85 * (sy - 0.30) / 0.70
        h = (0.050 + 0.030 * rnd.random()) * hs * persp
        R = rz(rnd.uniform(0, 6.283)) @ tilt(rnd.uniform(-0.05, 0.05), rnd.uniform(-0.05, 0.05))
        P = np.array([g.x, g.y, g.z - 0.004])
        buckets[typ].setdefault(col, []).append((P, R, h))
        stems.append((P, R, h))
        made += 1

    def m(name, rgb):
        mm = bpy.data.materials.get(name)
        if mm is None:
            mm = bpy.data.materials.new(name)
            mat_petal(mm, tuple(srgb2lin(c) for c in rgb))
        return mm

    for typ in ("A", "B"):
        hv, hfc = (A[0] if typ == "A" else B[0])
        for col, tfl in buckets[typ].items():
            merge(hv, hfc, tfl, "p16_flw%s_%s" % (typ, col), m("p16_mat%s_%s" % (typ, col), HUE[col]))
    if stems:
        merge(A[1][0], A[1][1], stems, "p16_stemA", m("p16_mstem", (0.16, 0.30, 0.11)))
    sd = float(cfg.get("stem_decimate", 0.18))
    ob = bpy.data.objects.get("p16_stemA")
    if ob is not None and sd < 1.0:
        bpy.context.view_layer.objects.active = ob
        md = ob.modifiers.new("dec", "DECIMATE")
        md.ratio = sd
        bpy.ops.object.modifier_apply(modifier=md.name)
    print("P15_FLOWERS_TRACED", made)

# ------------------------------------------------------------- golden light
def golden_lighting(scene, cfg):
    """warm low sun + cool sky fill + soft rim, AO, contact shadows, light haze"""
    scene.render.engine = "BLENDER_EEVEE"
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception:
        pass
    ev = scene.eevee
    ev.taa_render_samples = cfg.get("samples", 64)
    try:
        ev.use_raytracing = bool(cfg.get("raytracing", True))
        ev.ray_tracing_method = cfg.get("rt_method", "SCREEN")
        ev.use_fast_gi = bool(cfg.get("fast_gi", True))
        ev.indirect_light_intensity = cfg.get("indirect", 1.0)
    except Exception as e:
        print("P15_EEVEE_OPT_FAIL", str(e)[:80])
    for attr, val in (("use_shadows", True), ("use_raytracing", True), ("use_fast_gi", True),
                      ("use_volumetric_shadows", True)):
        try:
            setattr(ev, attr, val)
        except Exception:
            pass
    for attr, val in (("shadow_ray_count", cfg.get("shadow_rays", 4)),
                      ("shadow_step_count", cfg.get("shadow_steps", 12)),
                      ("shadow_resolution_scale", cfg.get("shadow_res", 1.0)),
                      ("fast_gi_distance", cfg.get("gi_dist", 1.6)),
                      ("fast_gi_quality", cfg.get("gi_quality", 1.0)),
                      ("fast_gi_ray_count", cfg.get("gi_rays", 2)),
                      ("fast_gi_step_count", cfg.get("gi_steps", 8)),
                      ("volumetric_tile_size", "4")):
        try:
            setattr(ev, attr, val)
        except Exception:
            pass

    # ---- world: cool deep-blue sky (matches the target's #183048 upper band)
    world = scene.world or bpy.data.worlds.new("P15World")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), nt.nodes.new("ShaderNodeOutputWorld"))
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
    # camera sees a deep navy sky (the target's own upper band is #183048-#406080);
    # the scene is lit by a brighter, warmer skylight so the island is not blue-grey.
    bg.inputs[0].default_value = (*L(cfg.get("sky", (0.16, 0.31, 0.50))), 1)
    bg.inputs[1].default_value = cfg.get("sky_strength", 0.85)
    nt.nodes.get("p15_bg_cam")  # noqa
    cam_bg = nt.nodes.get("p15_bg_cam") or nt.nodes.new("ShaderNodeBackground")
    cam_bg.name = "p15_bg_cam"
    cam_bg.inputs[0].default_value = (*L(cfg.get("sky_cam", (0.10, 0.20, 0.35))), 1)
    cam_bg.inputs[1].default_value = cfg.get("sky_cam_strength", 1.0)
    lp = nt.nodes.get("p15_lp") or nt.nodes.new("ShaderNodeLightPath")
    lp.name = "p15_lp"
    mx = nt.nodes.get("p15_bg_mix") or nt.nodes.new("ShaderNodeMixShader")
    mx.name = "p15_bg_mix"
    out_w = next(n for n in nt.nodes if n.type == "OUTPUT_WORLD")
    nt.links.new(lp.outputs["Is Camera Ray"], mx.inputs[0])
    nt.links.new(bg.outputs[0], mx.inputs[1])
    nt.links.new(cam_bg.outputs[0], mx.inputs[2])
    nt.links.new(mx.outputs[0], out_w.inputs["Surface"])

    # ---- three-point sun rig
    rig = (("_p15_key", (1.0, 0.83, 0.58), cfg.get("key", 4.2), cfg.get("key_rot", (58, 4, -34)), 6.0),
           ("_p15_fill", (0.55, 0.70, 1.0), cfg.get("fill", 0.9), (-46, 0, 128), 24.0),
           ("_p15_rim", (1.0, 0.86, 0.66), cfg.get("rim", 1.5), (-24, 0, 176), 14.0))
    for name, col, energy, rot, angle in rig:
        ld = bpy.data.lights.get(name) or bpy.data.lights.new(name, "SUN")
        ld.energy = energy
        ld.color = col
        try:
            ld.angle = math.radians(angle)
        except Exception:
            pass
        lo = bpy.data.objects.get(name)
        if lo is None:
            lo = bpy.data.objects.new(name, ld)
            scene.collection.objects.link(lo)
        lo.data = ld
        lo.rotation_euler = [math.radians(a) for a in rot]
        lo.hide_select = True

    scene.view_settings.exposure = cfg.get("exposure", 0.0)

    # ---- haze over the water (thin volume box, low z, only over the sea ring)
    if cfg.get("haze", 1):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.15))
        hz = bpy.context.active_object
        hz.name = "p15_haze"
        hz.scale = (cfg.get("haze_x", 26.0), cfg.get("haze_y", 26.0), cfg.get("haze_z", 0.30))
        hm = bpy.data.materials.new("p15_haze")
        hm.use_nodes = True
        hn = hm.node_tree
        hn.nodes.clear()
        o = hn.nodes.new("ShaderNodeOutputMaterial")
        pv = hn.nodes.new("ShaderNodeVolumePrincipled")
        pv.inputs["Density"].default_value = cfg.get("haze_density", 0.020)
        pv.inputs["Color"].default_value = (*L((0.80, 0.86, 0.95)), 1)
        pv.inputs["Anisotropy"].default_value = 0.35
        hn.links.new(pv.outputs[0], o.inputs["Volume"])
        hz.data.materials.append(hm)
        for p in hz.data.polygons:
            p.use_smooth = False
        print("P15_HAZE", cfg.get("haze_density", 0.020))


# ------------------------------------------------------------------ render
def setup_camera(scene, cam):
    cd = bpy.data.cameras.get("_p15_cam") or bpy.data.cameras.new("_p15_cam")
    co = bpy.data.objects.get("_p15_cam")
    if co is None:
        co = bpy.data.objects.new("_p15_cam", cd)
        scene.collection.objects.link(co)
    scene.camera = co
    cd.lens = 50.0
    aspect = cam.get("aspect", 16 / 9)
    cd.sensor_fit = "HORIZONTAL"
    cd.sensor_width = 36.0
    cd.angle_x = math.radians(cam["fov_deg"])
    cd.clip_end = 400.0
    eye = Vector(cam["eye"]); tgt = Vector(cam["target"])
    co.location = eye
    co.rotation_euler = (tgt - eye).to_track_quat("-Z", "Y").to_euler()
    return aspect


def render(scene, cam, label, size):
    aspect = setup_camera(scene, cam)
    scene.render.resolution_x = size
    scene.render.resolution_y = int(round(size / aspect))
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    path = os.path.join(OUTDIR, "%s.png" % label)
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("P15_RENDER", path)
    return path


def bake_basecolors():
    """glTF only stores factors/textures, not shader graphs. Give every procedural material a
    representative base colour (the midpoint of its own ColorRamp) so the exported GLB is not
    grey. The live Blender scene keeps the full procedural graph."""
    n = 0
    for m in list(bpy.data.materials):
        if not m.users or not m.use_nodes:
            continue
        nt = m.node_tree
        b = nt.nodes.get("Principled BSDF")
        if not b:
            continue
        sock = b.inputs["Base Color"]
        if not sock.is_linked:
            continue
        src = sock.links[0].from_node
        col = None
        if src.type == "VALTORGB":
            els = src.color_ramp.elements
            acc = [0.0, 0.0, 0.0]
            for e in els:
                c = e.color
                acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
            k = len(els)
            col = (acc[0] / k, acc[1] / k, acc[2] / k)
        elif src.type in ("MIX", "MIX_RGB"):
            rgbs = [i.default_value for i in src.inputs if i.type == "RGBA"]
            if rgbs:
                acc = [0.0, 0.0, 0.0]
                for c in rgbs:
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
                k = len(rgbs)
                col = (acc[0] / k, acc[1] / k, acc[2] / k)
        if col:
            for lk in list(sock.links):
                nt.links.remove(lk)
            sock.default_value = (*col, 1)
            m.diffuse_color = (*col, 1)
            n += 1
    print("P15_BAKED_BASECOLOR", n)


def export_glb(path):
    """keep only the deliverable scene: hide helper objects, then export"""
    for name in ("p15_haze",):
        o = bpy.data.objects.get(name)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
    for name in ("_p15_key", "_p15_fill", "_p15_rim", "_p15_cam", "_pipe_key", "_pipe_fill", "_pipe_rim", "_pipe_cam"):
        o = bpy.data.objects.get(name)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
    kw = dict(filepath=path, export_format="GLB", export_apply=True, export_yup=True, use_selection=False)
    try:                                     # Draco keeps a 4.7M-tri scene portable
        bpy.ops.export_scene.gltf(**kw,
                                  export_draco_mesh_compression_enable=True,
                                  export_draco_mesh_compression_level=6,
                                  export_draco_position_quantization=12,
                                  export_draco_normal_quantization=10,
                                  export_draco_texcoord_quantization=10)
        print("P15_EXPORT", path, os.path.getsize(path), "draco")
    except Exception as e:
        print("P15_EXPORT_DRACO_FAIL", str(e)[:120])
        bpy.ops.export_scene.gltf(**kw)
        print("P15_EXPORT", path, os.path.getsize(path), "plain")
    print("P15_EXPORT", path, os.path.getsize(path))


# -------------------------------------------------------------------- main
def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="{}")
    ap.add_argument("--tag", default="r510_p15")
    ap.add_argument("--size", type=int, default=1200)
    ap.add_argument("--views", default="ref")
    ap.add_argument("--skip", default="")
    ap.add_argument("--save", default="")
    a = ap.parse_args(argv)
    cfg = json.loads(a.cfg)
    skip = set(x for x in a.skip.split(",") if x)

    clear_scene()
    bpy.ops.import_scene.gltf(filepath=GLB)
    print("P15_IMPORT ok")

    if "tiered" not in skip:
        kill_tiered_props()
    if "foliage" not in skip:
        break_up_foliage(cfg)
    if "apron" not in skip:
        build_apron(cfg)
    cam = CAM_REF
    bvh = build_bvh()
    if "mats" not in skip:
        build_materials()

    # ---- phase 16: everything below is placed by *traced screen coordinate* (see LAYOUT)
    if "villa" not in skip:
        assemble_villa(bvh, cam, cfg)
    if "roadside" not in skip:
        assemble_props(bvh, cam, cfg)
    if "cypress" not in skip:
        assemble_cypress(bvh, cam, cfg, make_cypress_proto(cfg))
    if "animals" not in skip:
        assemble_horses(bvh, cam, cfg)
        assemble_rabbits(bvh, cam, cfg)
    if "flowers" not in skip:
        flower_mass_traced(bvh, cam, cfg)
    if "undergrowth" not in skip:
        undergrowth(bvh, cfg)
    if "branches" not in skip:
        framing_branches(cfg, cam)

    scene = bpy.context.scene
    if "light" not in skip:
        golden_lighting(scene, cfg)

    if cfg.get("_sweep"):
        # sweep lighting params in one session: [{name, overrides}]
        outs = []
        base = dict(cfg)
        for i, ov in enumerate(cfg["_sweep"]):
            merged = dict(base); merged.update(ov)
            golden_lighting(scene, merged)
            c2 = ov.get("_cam") or cam
            outs.append(render(scene, c2, "%s_sw%d" % (a.tag, i), a.size // 2))
            print("P15_SWEEP", i, json.dumps(ov))
        print("P15_DONE", json.dumps(outs))
        return

    if cfg.get("_blockout"):
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.show_shadows = True
        scene.display.shading.show_cavity = True
        print("P15_BLOCKOUT", json.dumps(render(scene, cam, a.tag + "_blockout", a.size)))
        return

    outs = []
    views = [v for v in a.views.split(",") if v]
    for v in views:
        c = CAM_CHAMP if v == "champ" else cam
        outs.append(render(scene, c, "%s_%s" % (a.tag, v), a.size))

    if cfg.get("_export"):
        bake_basecolors()
        export_glb(cfg["_export"])
    if a.save:
        bpy.ops.wm.save_as_mainfile(filepath=a.save)
        print("P15_SAVED", a.save)
    print("P15_DONE", json.dumps(outs))


if __name__ == "__main__":
    main()

# ===================================================== phase 16: assemble to the target
# Every entry is a *screen* coordinate read off references/morris_island_target.png.
# screen_to_ground() unprojects it through the camera and drops it on the terrain, so the
# props land where the target puts them instead of wherever the generator felt like.
LAYOUT = {
    #                x%      y%     note (target reading)
    "villa":       (0.548, 0.330),   # stone villa + tower, upper third, slightly right
    "gate":        (0.492, 0.560),   # wooden gate at the head of the path
    "bridge":      (0.520, 0.648),   # stone arch bridge over the stream
    "aqueduct":    (0.900, 0.372),   # distant aqueduct, far right
    "horse_white": (0.205, 0.640),   # white horse, left meadow
    "horse_brown": (0.312, 0.572),   # brown horse, centre-left
    "horse_far":   (0.848, 0.628),   # grey/white horse, right
    "rabbit_a":    (0.145, 0.845),
    "rabbit_b":    (0.680, 0.790),
}
# cluster definitions read off the target: cypress groves flanking the villa
CYP_CLUSTERS = [
    (0.432, 0.395, 10, 0.075),   # left of villa
    (0.520, 0.300, 7,  0.060),   # behind villa
    (0.634, 0.360, 9,  0.070),   # right of villa
    (0.375, 0.470, 5,  0.055),
    (0.700, 0.455, 6,  0.060),
]
# flower field regions: dominant colour per region, read off the target
FLOWER_REGIONS = [
    (0.50, 0.90, 0.60, "mixed"),     # foreground carpet
    (0.22, 0.75, 0.30, "white"),     # left meadow: daisies dominate
    (0.80, 0.78, 0.28, "scarlet"),   # right meadow: red tulips
    (0.50, 0.68, 0.30, "white"),
    (0.12, 0.60, 0.20, "orange"),
    (0.90, 0.55, 0.20, "red"),
]
