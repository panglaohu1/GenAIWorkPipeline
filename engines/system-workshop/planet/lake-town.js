/** REA house modules in a flat lake patch, anchored to the original spherical terrain. */
import {storeyHouseModule} from '../../REACodeRefactoring/src/sphere/planet-town.js';
const dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0),sub=(a,b)=>a.map((x,i)=>x-b[i]);
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const norm=a=>{const l=Math.hypot(...a);return a.map(x=>x/l)};
export function raySurface(mesh,origin,direction){
 let best=Infinity;const idx=mesh.indices||Array.from({length:mesh.positions.length/3},(_,i)=>i),p=mesh.positions;
 for(let i=0;i<idx.length;i+=3){const a=p.slice(idx[i]*3,idx[i]*3+3),b=p.slice(idx[i+1]*3,idx[i+1]*3+3),c=p.slice(idx[i+2]*3,idx[i+2]*3+3),e=sub(b,a),f=sub(c,a),n=cross(e,f),den=dot(n,direction);if(Math.abs(den)<1e-10)continue;
  const t=dot(n,sub(a,origin))/den;if(t<0)continue;const point=origin.map((x,k)=>x+direction[k]*t),q=sub(point,a),ee=dot(e,e),ef=dot(e,f),ff=dot(f,f),qe=dot(q,e),qf=dot(q,f),det=ee*ff-ef*ef;if(Math.abs(det)<1e-12)continue;
  const u=(ff*qe-ef*qf)/det,v=(ee*qf-ef*qe)/det;if(u>=-1e-7&&v>=-1e-7&&u+v<=1+1e-7)best=Math.min(best,t);
 }return best;
}
export function surfaceHeight(mesh,d,R){const t=raySurface(mesh,[0,0,0],d);return Number.isFinite(t)?t-R:-Infinity}
export function lakeCatalog(planet){return (planet.inlandWater||[]).map((w,index)=>({index,kind:w.kind,triangles:w.indices.length/3})).filter(w=>w.kind==='lake')}
export function buildLakeTown(planet,{lakeIndex,count=8,floors=2,terrainVersion,sceneId='lake-town',placements=null}){
 if(placements!==null&&(!Array.isArray(placements)||placements.length>64))throw Error('编辑布局最多64栋');
 if(!Number.isInteger(count)||count<1||count>32||!Number.isInteger(floors)||floors<1||floors>5)throw Error('建筑数量1–32、层数1–5');
 const water=planet.inlandWater?.[lakeIndex];if(!water||water.kind!=='lake')throw Error('请选择已生成水面的湖泊');
 const frame=water.frame;if(frame?.kind!=='local-tangent-plane')throw Error('这是旧版曲面湖泊，请先在球系统点击「旧湖转为平面湖区」');
 const R=planet.recipe.radius,{normal:n,tangentU:u,tangentV:v,origin}=frame;
 const map=(x,z,h)=>origin.map((c,i)=>c+u[i]*x+v[i]*z+n[i]*h);
 const positions=[],colors=[],buildings=[],palette=[[.85,.49,.35],[.89,.77,.48],[.45,.63,.67],[.61,.68,.48],[.67,.56,.7]],stone=[.61,.62,.57];
 function triangle(points,color){for(const p of points){positions.push(...p);colors.push(...color)}}
 function quad(a,b,c,d,color){triangle([a,b,c],color);triangle([a,c,d],color)}
 const candidates=[];
 if(placements!==null){const ids=new Set();for(const item of placements){
  if(!item||typeof item.id!=='string'||ids.has(item.id)||!Number.isFinite(item.x)||!Number.isFinite(item.z)||Math.abs(item.x)>100||Math.abs(item.z)>100||!Number.isInteger(item.floors)||item.floors<1||item.floors>5||!Number.isInteger(item.color)||item.color<0||item.color>=palette.length)throw Error('无效建筑编辑参数');
  if(candidates.some(other=>Math.abs(other.x-item.x)<3.599&&Math.abs(other.z-item.z)<3.599))throw Error('建筑台基重叠，请换一个位置');ids.add(item.id);candidates.push(item);
 }}else{for(let z=-8;z<=8;z++)for(let x=-8;x<=8;x++)candidates.push({x:x*3.6,z:z*3.6,floors,color:0});candidates.sort((a,b)=>Math.hypot(a.x,a.z)-Math.hypot(b.x,b.z));}
 for(const item of candidates){if(placements===null&&buildings.length>=count)break;const {x,z}=item,nf=item.floors,colorIndex=placements===null?buildings.length%palette.length:item.color;
  const corners=[[-1.8,-1.8],[1.8,-1.8],[1.8,1.8],[-1.8,1.8]].map(([a,b])=>[x+a,z+b]);
  const probes=[...corners,[x,z],...[[-1.8,0],[1.8,0],[0,-1.8],[0,1.8]].map(([a,b])=>[x+a,z+b])];
  const sampled=probes.map(([a,b])=>{const start=map(a,b,40),down=n.map(t=>-t);return {ground:40-raySurface(planet.mesh,start,down),water:40-raySurface(water,start,down)}});
  if(sampled.some(q=>!Number.isFinite(q.ground)||!Number.isFinite(q.water)||Math.abs(q.water)>.001||q.ground>=-.12)){if(placements!==null)throw Error('建筑位置超出有效湖面，或台基没有湖底承托');continue;}
  const level=.45,top=corners.map(([a,b])=>map(a,b,level)),bottom=corners.map(([a,b],i)=>map(a,b,sampled[i].ground-.2));
  quad(...top,stone);for(let k=0;k<4;k++)quad(bottom[k],bottom[(k+1)%4],top[(k+1)%4],top[k],stone);
  for(const tri of storeyHouseModule(4,nf,{seed:buildings.length})){const color=tri.part==='roof'?[.69,.27,.15]:tri.part==='window'||tri.part==='door'?[.16,.25,.29]:tri.part==='plinth'||tri.part==='trim'?stone:palette[colorIndex];
   triangle(tri.v.map(([a,b,h])=>map(x+(a-b)*Math.SQRT1_2*2.25,z+(a+b)*Math.SQRT1_2*2.25,level+h*2.2)),color)}
  buildings.push({id:item.id||sceneId+'-'+buildings.length,color:colorIndex,direction:n,localPosition:[x,level,z],foundation:{top:level,bottom:sampled.slice(0,4).map(q=>q.ground-.2),supportPoints:bottom,deckPoints:top,bearingDepth:.2,waterClearance:.45},floors:nf});
 }
 if(!buildings.length&&placements===null)throw Error('湖面没有足够的连续水域容纳建筑基座；请扩大湖泊范围或换一个湖');
 return {id:sceneId,terrainVersion,lakeIndex,center:n,frame,mesh:{positions,colors},buildings,requestedCount:placements===null?count:placements.length,checks:{finite:positions.every(Number.isFinite),allFoundationsSupported:buildings.every(b=>b.foundation.bearingDepth>=.19),allDecksAboveWater:buildings.every(b=>b.foundation.waterClearance>=.44),planarLake:true,scope:'flat local patch geometry and foundations only; gameplay and navigation not verified'},capability:{id:'planet.lake-town',source:'engines/REACodeRefactoring/src/sphere/planet-town.js#storeyHouseModule',adapter:'engines/system-workshop/planet/lake-town.js'}};
}
