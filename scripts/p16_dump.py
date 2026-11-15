"""Dump world-space bboxes of every object in the delivered composite, so the ortho
camera can be solved analytically instead of by eye."""
import bpy, json, sys

P = "/Users/panglaohu/Downloads/GenAIWorkPipeline"
RUN = P + "/runs/20261026-202458-9ac6"
GLB = RUN + "/blender/export/scene_composite.pre_phase15_20261029_000637.glb"
OUT = "/tmp/p16/bbox.json"

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB)
res = {}
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    mw = o.matrix_world
    vs = [mw @ v.co for v in o.data.vertices]
    if not vs:
        continue
    res[o.name] = [min(p.x for p in vs), min(p.y for p in vs), min(p.z for p in vs),
                   max(p.x for p in vs), max(p.y for p in vs), max(p.z for p in vs), len(vs)]
json.dump(res, open(OUT, "w"))
print("P16_DUMP", len(res), "objects ->", OUT)
