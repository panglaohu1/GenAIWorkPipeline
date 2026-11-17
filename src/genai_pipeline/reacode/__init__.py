"""Project-owned REACode engine: actual WorldMesh, exported to Blender coordinates."""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from .. import config
from ..capabilities.bridge import node_path

HERE = Path(__file__).parent

def build_mesh(state, seed=0):
    from ..capabilities.voxel import state_world
    world = state_world(state)
    if any(v & ~4095 for v in world['solid']):
        raise ValueError('REACode World supports 12 levels; reduce program height before building')
    root = config.resolve('engines/REACodeRefactoring')
    node = node_path()
    if not node:
        raise RuntimeError('REACode requires local Node')
    extent = max(max(abs(c['cx']), abs(c['cz'])) for c in world['cells'])
    radius = 10
    payload = dict(root=str(root), world=world, seed=seed, radius=radius,
                   hexSize=max(.1, extent / (radius*.85)), level_h=state.level_h)
    env = dict(os.environ, GENAI_REA_THREE=str(root/'vendor/three.module.js'))
    result = subprocess.run([node, '--experimental-loader', str(HERE/'loader.mjs'), str(HERE/'mesh.mjs')],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=120, env=env)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    data = json.loads(result.stdout)
    xyz, rgb, idx = data['vertices'], data['colors'], data['indices']
    vertices = [[xyz[i], -xyz[i+2], xyz[i+1]] for i in range(0,len(xyz),3)]
    faces = [idx[i:i+3] for i in range(0,len(idx),3)]
    # REA colors are already linear. Blender consumes these as vertex colors.
    colors = [[sum(rgb[v*3+k] for v in f)/3 for k in range(3)] for f in faces]
    return {'parts': {'rea_surface': {'v': vertices, 'f': faces}}, 'linear_colors': {'rea_surface': colors},
            'engine': 'REACodeRefactoring', 'cells':data['cells'], 'triangles':data['triangles'],
            'tiles':{}, 'wfc':{'engine':'REA modules'}, 'props':[], 'segments':0}
