import {pathToFileURL} from 'node:url';
import path from 'node:path';
let raw=''; for await(const c of process.stdin) raw+=c;
try {
 const p=JSON.parse(raw), imp=r=>import(pathToFileURL(path.join(p.root,r)).href);
 const {buildQuadGrid}=await imp('src/core/grid.js');
 const {decorateCells}=await imp('src/core/spatial.js');
 const {World}=await imp('src/core/world.js');
 const {WorldMesh}=await imp('src/world/worldmesh.js');
 const grid=decorateCells(buildQuadGrid({radius: p.radius, hexSize:p.hexSize,seed:p.seed,relaxPasses:40}));
 const w=new World(grid), src=p.world;
 const {PALETTES}=await imp('src/core/palette.js');w.palette=PALETTES.pastel;
 for(const c of w.cells){
   let best=null, dist=Infinity;
   for(const v of src.cells){const d=(v.cx-c.cx)**2+(v.cz-c.cz)**2;if(d<dist){dist=d;best=v.id;}}
   if(best!==null && dist<p.hexSize**2*4){w.solid[c.id]=src.solid[best];w.rock[c.id]=src.rock[best];w.color[c.id]=Math.max(-1,src.color[best]%15);}
 }
 w.touch(); const wm=new WorldMesh(w,{levelHeight:p.level_h}); wm.rebuild();
 const pos=[...wm.geometry.attributes.position.array], color=[...wm.geometry.attributes.color.array];
 if(!pos.length||!pos.every(Number.isFinite)) throw Error('empty/nonfinite mesh');
 const idx=wm.geometry.index?[...wm.geometry.index.array]:Array.from({length:pos.length/3},(_,i)=>i);
 process.stdout.write(JSON.stringify({vertices:pos,colors:color,indices:idx,cells:w.cells.length,triangles:idx.length/3}));
} catch(e) {process.stderr.write(String(e.stack));process.exitCode=1;}
