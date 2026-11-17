/** Local reconstructed terrain editing capabilities; operates on the actual sampled sphere. */
export const TERRAIN_EDIT_CAPABILITIES = [
 ['mountain','山川','向上塑形'],['land','陆地','抬升地表'],['ocean','海洋','压低海床'],
 ['lake','湖泊','挖掘湖盆并生成水面'],['river','河流','沿折线开槽并生成下行水面']
].map(([id,name,does])=>({id:'planet.edit.'+id,name,does,source:'engines/system-workshop/planet/terrain-edit.js'}));
const dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0);
const norm=a=>{const l=Math.hypot(...a);return a.map(x=>x/l)};
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const angle=(a,b)=>Math.acos(Math.max(-1,Math.min(1,dot(a,b))));
function validate(op){
 if(!TERRAIN_EDIT_CAPABILITIES.some(c=>c.id==='planet.edit.'+op.tool))throw Error('未知地形能力');
 for(const [k,min,max] of [['radius',2,60],['strength',0.25,20],['waterLevel',-10,30]])if(!Number.isFinite(op[k])||op[k]<min||op[k]>max)throw Error('地形参数越界：'+k);
 if(!Array.isArray(op.points)||op.points.length<(op.tool==='river'?2:1)||op.points.length>32||op.points.some(d=>!Array.isArray(d)||d.length!==3||!d.every(Number.isFinite)||Math.hypot(...d)<.5))throw Error('需要有效的球面位置；河流至少两个点');
}
export function editTerrain(input,op){
 if(op?.tool==='flatten_lakes')return flattenLakes(input);
 validate(op);
 const d=structuredClone(input),R=d.recipe.radius,sea=d.recipe.seaLevel,positions=d.mesh.positions;
 const dirs=[],heights=[];for(let i=0;i<positions.length;i+=3){const p=positions.slice(i,i+3);dirs.push(norm(p));heights.push(Math.hypot(...p)-R)}
 const points=op.points.map(norm),samples=[];
 const lakeNormal=points[0],planeLevel=d.recipe.radius+op.waterLevel;
 function heightAt(v){let k=0,s=-Infinity;for(let i=0;i<dirs.length;i++){const q=dot(v,dirs[i]);if(q>s){s=q;k=i}}return heights[k]}
 if(op.tool==='river'){
  let level=heightAt(points[0]);
  for(let i=1;i<points.length;i++){
   const a=points[i-1],b=points[i],arc=angle(a,b);if(arc>Math.PI*.75)throw Error('河段太长，请增加中间控制点');
   const count=Math.max(2,Math.ceil(arc*R/Math.max(.5,op.radius/3))),end=Math.min(level,heightAt(b));
   for(let j=0;j<=count;j++){const t=j/count,v=norm(a.map((x,k)=>x*(1-t)+b[k]*t));samples.push({v,level:level+(end-level)*t})}level=end;
  }
 }else for(const v of points)samples.push({v,level:op.waterLevel});
 const waterLevels=new Array(dirs.length).fill(null);let changed=0;
 for(let i=0;i<dirs.length;i++){
  let nearest=null,dist=Infinity;for(const s of samples){const a=angle(dirs[i],s.v)*R;if(a<dist){dist=a;nearest=s}}
  if(dist>=op.radius)continue;
  const w=(1-(dist/op.radius)**2)**2,old=heights[i];let h=old;
  if(op.tool==='mountain'||op.tool==='land')h+=op.strength*w;
  else if(op.tool==='ocean')h=Math.min(h,old*(1-w)+(sea-op.strength)*w);
  else if(op.tool==='lake'){
   const u=dist/op.radius,level=planeLevel/dot(lakeNormal,dirs[i])-R;
   if(u<.72){const bowl=(1-(u/.72)**2)**2;h=Math.min(h,level-op.strength*bowl);if(h<level-.03)waterLevels[i]=level}
   else {const rim=Math.sin(Math.PI*(u-.72)/.28);h=Math.max(h,old+(Math.max(0,level+.25-old))*rim)}
  }else {const level=nearest.level,depth=op.strength*w;h=Math.min(h,level-depth);if(h<level-.03)waterLevels[i]=level}
  if(Math.abs(h-old)>1e-6)changed++;heights[i]=h;positions.splice(i*3,3,...dirs[i].map(x=>x*(R+h)));
 }
 if(!changed&&waterLevels.every(v=>v===null))throw Error('笔刷范围内无可修改地形，请调整范围或深度');
 // Cut water polygons against the edited terrain, avoiding flat water cards over shores.
 const water={positions:[],indices:[],kind:op.tool},ids=d.mesh.indices;
 if(op.tool==='lake'){const tangentU=norm(cross(Math.abs(lakeNormal[1])>.9?[1,0,0]:[0,1,0],lakeNormal));water.frame={normal:lakeNormal,tangentU,tangentV:cross(lakeNormal,tangentU),origin:lakeNormal.map(x=>x*(planeLevel+.04)),waterLevel:op.waterLevel,brushRadius:op.radius,kind:'local-tangent-plane'};}
 if(op.tool==='lake'||op.tool==='river')for(let i=0;i<ids.length;i+=3){
  const tri=ids.slice(i,i+3),active=tri.filter(k=>waterLevels[k]!==null);if(!active.length)continue;
  const fallback=active.reduce((s,k)=>s+waterLevels[k],0)/active.length;
  let poly=tri.map(k=>({dir:dirs[k],level:waterLevels[k]??fallback,depth:waterLevels[k]===null?-.05:waterLevels[k]-heights[k]}));
  const clipped=[];for(let j=0;j<poly.length;j++){const a=poly[j],b=poly[(j+1)%poly.length];if(a.depth>0)clipped.push(a);if((a.depth>0)!==(b.depth>0)){const t=a.depth/(a.depth-b.depth);clipped.push({dir:norm(a.dir.map((x,k)=>x+(b.dir[k]-x)*t)),level:a.level+(b.level-a.level)*t,depth:0})}}
  if(clipped.length<3)continue;const start=water.positions.length/3;for(const p of clipped)water.positions.push(...p.dir.map(x=>x*(op.tool==='lake'?(planeLevel+.04)/dot(lakeNormal,p.dir):R+p.level+.04)));for(let j=1;j<clipped.length-1;j++)water.indices.push(start,start+j,start+j+1);
 }
 // Land and water share the depth buffer; raised ground naturally occludes earlier water surfaces.
 const previous=d.inlandWater||[];d.inlandWater=[...previous, ...(water.indices.length?[water]:[])];
 d.mesh.colors=[];let land=0,min=Infinity,max=-Infinity;
 for(let i=0;i<heights.length;i++){const h=heights[i];if(h>sea)land++;min=Math.min(min,h);max=Math.max(max,h);d.mesh.colors.push(...(h<=sea?[.28,.53,.63]:h>10?[.82,.85,.81]:h>6?[.51,.56,.52]:[.55,.68,.44]))}
 for(const r of d.regions){r.height=heightAt(r.direction);r.land=r.height>sea}
 d.stats={...d.stats,minHeight:min,maxHeight:max,sampledLandRatio:land/heights.length};
 d.baseCompilerChecks??=d.checks;
 d.checks={closed:input.checks.closed,euler:input.checks.euler,finite:positions.every(Number.isFinite),compiler:false,reports:{},edited:true};
 d.navigation={nodes:0,routes:0,scope:'manual terrain changed; compiler navigation invalidated; rebuild required'};
 d.hashes={};d.capabilities??=[];d.capabilities.push({id:'planet.edit.'+op.tool,source:'engines/system-workshop/planet/terrain-edit.js',params:op,changedVertices:changed,waterTriangles:water.indices.length/3});
 d.editRevision=(d.editRevision||0)+1;return d;
}

export function flattenLakes(input){
 const lakes=(input.inlandWater||[]).map((w,index)=>({w,index})).filter(({w})=>w.kind==='lake'&&w.frame?.kind!=='local-tangent-plane');
 if(!lakes.length)throw Error('所有湖泊已是平面湖区，无需转换');
 let result=structuredClone(input);const ops=(input.capabilities||[]).filter(c=>c.id==='planet.edit.lake');
 for(const {w,index}of lakes){const ordinal=input.inlandWater.slice(0,index).filter(w=>w.kind==='lake').length;const prior=ops[ordinal]?.params;const c=[0,0,0];let level=0;for(let i=0;i<w.positions.length;i+=3){const p=w.positions.slice(i,i+3);for(let k=0;k<3;k++)c[k]+=p[k];level+=Math.hypot(...p)-input.recipe.radius-.04}const center=prior?.points?.[0]||norm(c);level=prior?.waterLevel??level/(w.positions.length/3);let radius=2;for(let i=0;i<w.positions.length;i+=3)radius=Math.max(radius,angle(norm(w.positions.slice(i,i+3)),center)*input.recipe.radius*1.3);
  result=editTerrain(result,{tool:'lake',points:[center],radius:Math.min(60,prior?.radius??radius),strength:prior?.strength??4,waterLevel:Math.max(-10,Math.min(30,level))});
  const plane=result.inlandWater.pop();result.inlandWater[index]=plane;
 }
 return result;
}
