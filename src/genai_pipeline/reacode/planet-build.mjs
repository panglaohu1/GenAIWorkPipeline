import fs from 'node:fs';
import {compilePlanetV8} from '../../../engines/procgen/planet/planetCompilerV8.js';
import {execute} from './planet-library.mjs';
const {recipe:baseRecipe,out,steps=[]}=JSON.parse(fs.readFileSync(0,'utf8'));
const execution=execute(baseRecipe,steps),recipe=execution.recipe;
const t=performance.now(),r=compilePlanetV8({...recipe,landformChain:false});
if(!r.ok)throw Error(JSON.stringify({stage:r.stage,report:r.report}));
const lat=64,lon=128,R=recipe.radius,sea=recipe.seaLevel,positions=[],colors=[],indices=[],directions=[];
let land=0,min=Infinity,max=-Infinity;
function add(d){const q=r.field.semanticAt(d),h=q.height;if(!Number.isFinite(h))throw Error('nonfinite height');directions.push(d);positions.push(...d.map(v=>v*(R+h)));min=Math.min(min,h);max=Math.max(max,h);const dry=h>sea;if(dry)land++;const c=!dry?[.28,.53,.63]:q.snowness>.4?[.84,.88,.85]:q.rockness>.6?[.52,.57,.54]:q.forestness>.35?[.30,.48,.35]:[.55,.68,.44];colors.push(...c)}
add([0,1,0]);for(let j=1;j<lat;j++){const a=Math.PI*j/lat;for(let i=0;i<lon;i++){const b=2*Math.PI*i/lon;add([Math.sin(a)*Math.cos(b),Math.cos(a),Math.sin(a)*Math.sin(b)])}}add([0,-1,0]);
const idx=(j,i)=>1+(j-1)*lon+(i+lon)%lon;
function face(a,b,c){const A=directions[a],B=directions[b],C=directions[c],u=B.map((x,i)=>x-A[i]),v=C.map((x,i)=>x-A[i]),cross=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];if(cross.reduce((s,x,i)=>s+x*A[i],0)<0)[b,c]=[c,b];indices.push(a,b,c)}
for(let i=0;i<lon;i++)face(0,idx(1,i),idx(1,i+1));
for(let j=1;j<lat-1;j++)for(let i=0;i<lon;i++){face(idx(j,i),idx(j+1,i),idx(j+1,i+1));face(idx(j,i),idx(j+1,i+1),idx(j,i+1))}
for(let i=0;i<lon;i++)face(directions.length-1,idx(lat-1,i+1),idx(lat-1,i));
const edges=new Map();for(let i=0;i<indices.length;i+=3)for(let k=0;k<3;k++){let a=indices[i+k],b=indices[i+(k+1)%3];if(a>b)[a,b]=[b,a];const key=a+':'+b;edges.set(key,(edges.get(key)||0)+1)}
const closed=[...edges.values()].every(n=>n===2),euler=directions.length-edges.size+indices.length/3;
if(!closed||euler!==2)throw Error('global sphere not closed');
const norm=p=>{const l=Math.hypot(...p);return p.map(x=>x/l)},dot=(a,b)=>a.reduce((n,v,i)=>n+v*b[i],0),cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const regions=r.grid.main.faces.map((f,i)=>{const d=r.grid.dual.directionOf(i),q=r.field.semanticAt(d),up=Math.abs(d[1])>.9?[1,0,0]:[0,1,0],u=norm(cross(up,d)),v=cross(d,u);return {id:r.grid.main.faceIds[i],direction:d,height:q.height,land:q.height>sea,neighbors:r.grid.dual.neighborsOf(i).map(n=>r.grid.dual.cellId(n.to)),corners:f.map(k=>norm(r.grid.main.positions[k])),frame:{normal:d,tangentU:u,tangentV:v}}});
const reports=Object.fromEntries(Object.entries(r).filter(([k,v])=>k.endsWith('Report')&&v&&typeof v.ok==='boolean').map(([k,v])=>[k,{ok:v.ok,errors:v.errors||[]}]))
const data={recipe,capabilities:execution.trace,mesh:{positions,colors,indices},regions,checks:{compiler:r.ok,closed,euler,finite:positions.every(Number.isFinite),reports},stats:{vertices:directions.length,triangles:indices.length/3,regions:regions.length,sampledLandRatio:land/directions.length,minHeight:min,maxHeight:max,compileMs:performance.now()-t},hashes:{grid:r.grid.hash,wfc:r.wfc.solutionHash,hydrology:r.snapshot.hydrologyHash,climate:r.snapshot.climateHash,ecology:r.snapshot.ecologyHash},navigation:{nodes:r.navigation?.nodes?.length||0,routes:r.terrainRoutes?.routes?.length||0,scope:'compiler graph only; not player traversal validation'},terrainAnchors:r.manifest.map(m=>({id:m.id,direction:m.direction,kind:'inherited terrain constraint; no scene instance'}))};fs.writeFileSync(out,JSON.stringify(data));console.log(JSON.stringify({stats:data.stats,checks:data.checks}));
