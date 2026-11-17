import * as THREE from 'three';
const $=s=>document.querySelector(s),host=$('#globe');let id=new URLSearchParams(location.search).get('run'),data=null,version=null,selected=null,loaded=null,currentStatus=null,pollEpoch=0,pollTimer=null,creating=false;
const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));host.prepend(renderer.domElement);renderer.setClearColor('#e6f2f5');const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(40,1,.1,5000);scene.add(new THREE.HemisphereLight(0xffffff,0x64777c,2));const sun=new THREE.DirectionalLight(0xffffff,2);sun.position.set(3,5,4);scene.add(sun);let surface,ocean,wire,marker;let inlandObjects=[],townObjects=[],brushTool='select',brushPoints=[],brushMarkers=[],editing=false;let azimuth=.8,polar=1.05,distance=600,R=160,focusPoint=null;let lakeEditContext=null,selectionOutline=null;
function resize(){renderer.setSize(host.clientWidth,host.clientHeight,false);camera.aspect=host.clientWidth/host.clientHeight;camera.updateProjectionMatrix()}new ResizeObserver(resize).observe(host);
function animate(){requestAnimationFrame(animate);camera.position.set(distance*Math.sin(polar)*Math.cos(azimuth),distance*Math.cos(polar),distance*Math.sin(polar)*Math.sin(azimuth));if(focusPoint){camera.position.add(focusPoint);camera.lookAt(focusPoint)}else camera.lookAt(0,0,0);renderer.render(scene,camera);if(window.planetPreview?.ready)window.planetPreview.frameRendered=true}animate();
let drag=null;renderer.domElement.onpointerdown=e=>{
 if(lakeEditContext&&(e.button===0||e.button===2)){
  lakeStroke={cells:new Set()};
  lakeEditClick(e,e.button===2);
  renderer.domElement.setPointerCapture(e.pointerId);
  return;
 }
 drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY};renderer.domElement.setPointerCapture(e.pointerId)};renderer.domElement.onpointermove=e=>{
 if(lakeEditContext){
  lakeHover(e);
  if(lakeStroke){
   if(e.buttons&2)lakeStrokeStep(e,true);
   else if(e.buttons&1)lakeStrokeStep(e,false);
   return;
  }
 }
 if(!drag)return;azimuth-=(e.clientX-drag.x)*.006;polar=THREE.MathUtils.clamp(polar+(e.clientY-drag.y)*.006,.07,Math.PI-.07);drag.x=e.clientX;drag.y=e.clientY};renderer.domElement.onpointerup=e=>{
 if(lakeStroke){
  lakeStroke=null;
  return;
 }
 if(drag&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)<8&&surface){if(lakeEditContext){lakeEditClick(e);drag=null;return}const rect=renderer.domElement.getBoundingClientRect(),ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);const hit=ray.intersectObject(surface)[0];if(hit){const d=hit.point.normalize();if(brushTool!=='select'){addBrushPoint(d.toArray());drag=null;return}const region=data.regions.reduce((a,b)=>new THREE.Vector3(...a.direction).dot(d)>new THREE.Vector3(...b.direction).dot(d)?a:b);selectRegion(region.id)}}drag=null};renderer.domElement.onwheel=e=>{e.preventDefault();distance=THREE.MathUtils.clamp(distance*Math.exp(e.deltaY*.001),focusPoint?10:R*1.3,focusPoint?R*3:R*8)};renderer.domElement.addEventListener('wheel',e=>e.preventDefault(),{passive:false});
function selectRegion(regionId){selected=data.regions.find(r=>r.id===regionId);if(!selected)return;$('#regionSelect').value=selected.id;$('#region').replaceChildren();for(const text of ['区域 '+selected.id,selected.land?'陆地':'海域','地表高度 '+selected.height.toFixed(2),'邻接 '+selected.neighbors.join('、'),'场景实例：'+(data.sceneInstances||[]).filter(s=>data.regions.reduce((a,b)=>new THREE.Vector3(...a.direction).dot(new THREE.Vector3(...s.center))>new THREE.Vector3(...b.direction).dot(new THREE.Vector3(...s.center))?a:b).id===selected.id).length]){const p=document.createElement('p');p.textContent=text;$('#region').append(p)}if(marker){scene.remove(marker);marker.geometry.dispose();marker.material.dispose()}marker=new THREE.Mesh(new THREE.SphereGeometry(R*.018,12,8),new THREE.MeshBasicMaterial({color:0x087a89,depthTest:false}));marker.renderOrder=10;marker.visible=!new URLSearchParams(location.search).has('embed');marker.position.fromArray(selected.direction).multiplyScalar(R+selected.height+R*.02);scene.add(marker);$('#reserve').disabled=!version||data.checks.closed!==true||currentStatus!=='terrain_checked';window.planetPreview.selected=selected}
function load(payload){if(selectionOutline){scene.remove(selectionOutline);selectionOutline.geometry.dispose();selectionOutline.material.dispose();selectionOutline=null;}for(const o of inlandObjects){scene.remove(o);o.geometry.dispose();o.material.dispose()}inlandObjects=[];for(const o of townObjects){scene.remove(o);o.geometry.dispose();o.material.dispose()}townObjects=[];data=payload.planet;version=payload.state.version;R=data.recipe.radius;distance=R*3.2;focusPoint=null;for(const o of [surface,ocean,wire,marker])if(o){scene.remove(o);o.geometry.dispose();o.material.dispose()}marker=null;const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(data.mesh.positions,3));g.setAttribute('color',new THREE.Float32BufferAttribute(data.mesh.colors,3));g.setIndex(data.mesh.indices);g.computeVertexNormals();surface=new THREE.Mesh(g,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1}));scene.add(surface);ocean=new THREE.Mesh(new THREE.SphereGeometry(R+data.recipe.seaLevel+.08,128,64),new THREE.MeshStandardMaterial({color:0x74b3c5,roughness:.5}));scene.add(ocean);wire=new THREE.LineSegments(new THREE.WireframeGeometry(g),new THREE.LineBasicMaterial({color:0x386875,transparent:true,opacity:.25}));wire.visible=$('#wire').checked;scene.add(wire);for(const w of data.inlandWater||[]){const wg=new THREE.BufferGeometry();wg.setAttribute('position',new THREE.Float32BufferAttribute(w.positions,3));wg.setIndex(w.indices);wg.computeVertexNormals();const wm=new THREE.Mesh(wg,new THREE.MeshStandardMaterial({color:0x62acbf,roughness:.5,side:THREE.DoubleSide}));scene.add(wm);inlandObjects.push(wm)}$('#regionSelect').replaceChildren();for(const r of data.regions){const o=document.createElement('option');o.value=r.id;o.textContent=r.id+' · '+(r.land?'陆地':'海域');$('#regionSelect').append(o)}$('#stats').replaceChildren();const rows=[['半径',R],['顶点',data.stats.vertices],['三角面',data.stats.triangles],['全球区域',data.stats.regions],['采样陆地点占比',(100*data.stats.sampledLandRatio).toFixed(1)+'%'],['高度范围',data.stats.minHeight.toFixed(2)+'–'+data.stats.maxHeight.toFixed(2)],['导航图节点',data.navigation.nodes],['湖区场景',(data.sceneInstances||[]).length],['湖区建筑',(data.sceneInstances||[]).reduce((n,s)=>n+s.buildings.length,0)]];for(const [a,b]of rows){const el=document.createElement('div');el.textContent=a+'：'+b;$('#stats').append(el)}$('#checks').replaceChildren();for(const [a,b]of [['编译器',data.checks.compiler],['完整采样球面闭合',data.checks.closed],['有限坐标',data.checks.finite],['Euler = 2',data.checks.euler===2],...Object.entries(data.checks.reports).map(([k,v])=>[k,v.ok])]){const el=document.createElement('div');el.textContent=(b?'✓ ':'✕ ')+a;$('#checks').append(el)}$('#version').textContent=version||'等待版本';$('#hint').textContent='整颗星球 · 拖动旋转 · 点击选区';
for(const town of data.sceneInstances||[]){const tg=new THREE.BufferGeometry();tg.setAttribute('position',new THREE.Float32BufferAttribute(town.mesh.positions,3));tg.setAttribute('color',new THREE.Float32BufferAttribute(town.mesh.colors,3));tg.computeVertexNormals();const tm=new THREE.Mesh(tg,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1,side:THREE.DoubleSide}));tm.userData.sceneId=town.id;scene.add(tm);townObjects.push(tm)}
$('#linkedSceneTasks').replaceChildren();for(const task of data.sceneTasks||[]){const a=document.createElement('a');a.href='/static/reacode.html?'+new URLSearchParams({planet:id,terrain_version:task.binding.terrain_version,lake:task.binding.lake_index,scene_run:task.id});a.textContent='湖泊 '+(task.binding.lake_index+1)+' · '+task.id+' · '+task.status;a.style.display='block';$('#linkedSceneTasks').append(a)}if(!$('#linkedSceneTasks').children.length)$('#linkedSceneTasks').textContent='球做好后，选择湖区并进入场景管线。';
const previousLake=$('#lakeSelect').value;$('#lakeSelect').replaceChildren();for(const [i,w]of (data.inlandWater||[]).entries()){if(w.kind!=='lake')continue;const o=document.createElement('option');o.value=String(i);o.textContent='湖泊 '+(i+1)+' · '+(w.frame?'平面湖区':'旧版曲面')+' · '+w.indices.length/3+' 水面三角形';$('#lakeSelect').append(o)}if(previousLake&&[...$('#lakeSelect').options].some(o=>o.value===previousLake))$('#lakeSelect').value=previousLake;$('#buildLakeScene').disabled=!$('#lakeSelect').options.length;$('#focusLake').disabled=!$('#lakeSelect').options.length;
$('#lakeSceneStatus').textContent=(data.sceneInstances||[]).length?'当前球面已有 '+data.sceneInstances.reduce((n,s)=>n+s.buildings.length,0)+' 栋湖区建筑；可聚焦查看。':$('#lakeSelect').options.length?'湖面已就绪，可以直接建造水上建筑。':'先在左侧用湖泊笔刷生成湖面。';window.planetPreview={ready:true,data,version,selectRegion,get camera(){return camera}};selectRegion(data.regions.find(r=>r.land)?.id||data.regions[0].id);if(new URLSearchParams(location.search).has('lake')){const lake=new URLSearchParams(location.search).get('lake');if([...$('#lakeSelect').options].some(o=>o.value===lake))$('#lakeSelect').value=lake;focusLake()}}
async function api(url,opts){const r=await fetch(url,{cache:'no-store',...opts}),d=await r.json();if(!r.ok)throw Error(d.detail||r.status);return d}
const stateLabels={planned:'全球地形规划完成，等待编译',terrain_built:'地形已编译，正在自动检查',terrain_checked:'全球地形检查已通过，可以选区绑定',terrain_edited:'自由地形修改已保存；几何检查通过，原导航与场景适配需重建'};
async function poll(){
 const epoch=++pollEpoch;clearTimeout(pollTimer);
 try{
  if(!id){const latest=await api('/api/reacode/planet-latest');if(epoch!==pollEpoch)return;id=latest.id}
  if(!id){$('#status').textContent='尚无全球任务，请发起生成';return}
  const requestRun=id;
  const r=await api('/api/reacode/planet-data/'+encodeURIComponent(requestRun)+(new URLSearchParams(location.search).has('candidate')?'?candidate='+encodeURIComponent(new URLSearchParams(location.search).get('candidate')):''));
  if(epoch!==pollEpoch||requestRun!==id)return;
  currentStatus=r.state.status;
  const renderKey=requestRun+':'+r.state.version+':'+(r.planet.sceneInstances||[]).map(s=>s.sceneRevision||s.id).join(',');if(loaded!==renderKey){load(r);loaded=renderKey}
  $('#status').textContent=(stateLabels[currentStatus]||'正在处理全球地形')+'\n任务：'+requestRun+'\n'+(r.state.plan?.rationale||'');
  $('#reserve').disabled=currentStatus!=='terrain_checked'||!selected;
  $('#binding').textContent=currentStatus==='terrain_checked'?'检查已通过；选好区域后点击绑定。':currentStatus==='terrain_edited'?'手动地形已保存；原导航检查失效，后续场景绑定需重新检查。':'全球检查正在自动执行，请稍候。';
  $('#create').disabled=creating||!['terrain_checked','terrain_edited'].includes(currentStatus);
  if(!['terrain_checked','terrain_edited'].includes(currentStatus))pollTimer=setTimeout(poll,3000);
 }catch(e){if(epoch!==pollEpoch)return;currentStatus=null;$('#reserve').disabled=true;$('#status').textContent=e.message+'\n系统自动检查状态每3秒更新，无需重新生成。';pollTimer=setTimeout(poll,3000)}
}
$('#create').onclick=async()=>{if(creating)return;creating=true;$('#create').disabled=true;try{
 const f=new FormData();f.set('mode','rea_planet');f.set('title','Oskar 重构能力 · 全球球面地形');f.set('brief',$('#brief').value);f.set('options','{"model":"main","approval_gates":[]}');
 const r=await api('/api/runs',{method:'POST',body:f});++pollEpoch;clearTimeout(pollTimer);id=r.id;loaded=null;version=null;selected=null;currentStatus=null;data=null;
 for(const o of [surface,ocean,wire,marker])if(o){scene.remove(o);o.geometry.dispose();o.material.dispose()}surface=ocean=wire=marker=null;
 $('#regionSelect').replaceChildren();$('#region').textContent='新任务完成全球检查后可选区';$('#reserve').disabled=true;$('#version').textContent='等待新地形版本';$('#stats').textContent='新任务正在构建';$('#checks').textContent='等待本次任务检查';$('#binding').textContent='尚未选区';$('#hint').textContent='正在生成新任务的全球地形…';history.replaceState(null,'','?run='+id);poll();
 }catch(e){$('#status').textContent=e.message+'；生成任务需要登录原控制台。';$('#create').disabled=false}finally{creating=false}};
$('#refreshChecks').onclick=poll;
$('#regionSelect').onchange=()=>selectRegion($('#regionSelect').value);$('#wire').onchange=()=>{if(wire)wire.visible=$('#wire').checked};$('#reserve').onclick=async()=>{try{await poll();if(currentStatus!=='terrain_checked'||!selected)throw Error('全球检查尚未通过，请等待状态更新');const binding=await api('/api/reacode/planet-region/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region_id:selected.id,terrain_version:version})});localStorage.setItem('genai.rea.planet-binding',JSON.stringify(binding));$('#binding').textContent='区域已绑定到当前地形版本；局部场景尚未创建。'}catch(e){$('#binding').textContent=e.message}};poll();

$('#terrainSea').oninput=()=>{$('#seaValue').textContent=$('#terrainSea').value};
$('#applyTerrain').onclick=async()=>{
 const button=$('#applyTerrain'),feedback=$('#terrainFeedback');
 const recipe={seed:Number($('#terrainSeed').value),radius:Number($('#terrainRadius').value),seaLevel:Number($('#terrainSea').value)};
 if(!Number.isInteger(recipe.seed)||recipe.seed<1||recipe.seed>999||!Number.isFinite(recipe.radius)||recipe.radius<80||recipe.radius>400){feedback.textContent='种子需为1–999整数，半径需为80–400。';return}
 button.disabled=true;feedback.textContent='正在创建地形参数任务…';
 try{const form=new FormData();form.set('mode','rea_planet');form.set('title','球系统 · 地形菜单调整');form.set('brief','按地形菜单参数构建全球球面并自动检查。');form.set('options',JSON.stringify({model:'main',approval_gates:[],planet_recipe:recipe}));
 const result=await api('/api/runs',{method:'POST',body:form});location.assign('/static/planet-studio.html?run='+encodeURIComponent(result.id));
 }catch(e){feedback.textContent='未创建任务：'+e.message+'。如未登录，请先到控制台登录。';button.disabled=false}
};

for(const [slider,out] of [['brushRadius','radiusValue'],['brushStrength','strengthValue'],['brushWater','waterValue']])$('#'+slider).oninput=()=>$('#'+out).textContent=$('#'+slider).value;
function clearBrush(){brushPoints=[];for(const o of brushMarkers){scene.remove(o);o.geometry.dispose();o.material.dispose()}brushMarkers=[];$('#brushStatus').textContent='选点已清除'}
function addBrushPoint(direction){if(editing)return;if(brushTool!=='river')clearBrush();if(brushPoints.length>=32){$('#brushStatus').textContent='河流最多32个控制点';return}brushPoints.push(direction);const o=new THREE.Mesh(new THREE.SphereGeometry(R*.012,10,8),new THREE.MeshBasicMaterial({color:0xe18f35,depthTest:false}));o.position.fromArray(direction).multiplyScalar(R+data.stats.maxHeight+1);o.renderOrder=11;scene.add(o);brushMarkers.push(o);$('#brushStatus').textContent='已选 '+brushPoints.length+' 点；点击应用笔刷提交真实地形修改'}
for(const b of document.querySelectorAll('[data-tool]'))b.onclick=()=>{brushTool=b.dataset.tool;clearBrush();for(const other of document.querySelectorAll('[data-tool]'))other.setAttribute('aria-pressed',String(other===b));$('#brushStatus').textContent=brushTool==='select'?'点击球面选择区域':'已选择'+b.textContent+'；请点击球面选点'};
$('#clearBrush').onclick=clearBrush;
async function submitTerrainEdit(undo=false,operationOverride=null){
 if(editing||!data)return;
 if(!undo&&!operationOverride&&(brushTool==='select'||brushPoints.length<(brushTool==='river'?2:1))){$('#brushStatus').textContent='先选择工具和位置；河流需要至少两个点';return}
 editing=true;$('#applyBrush').disabled=$('#undoTerrain').disabled=true;clearTimeout(pollTimer);++pollEpoch;$('#brushStatus').textContent='本机执行能力并检查地形…';
 try{const body={terrain_version:version,undo,operation:operationOverride||{tool:brushTool,points:brushPoints,radius:Number($('#brushRadius').value),strength:Number($('#brushStrength').value),waterLevel:Number($('#brushWater').value)}};
 const result=await api('/api/reacode/planet-edit/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});currentStatus=result.state.status;load(result);loaded=id+':'+version;clearBrush();$('#brushStatus').textContent=undo?'已撤销，恢复上一个地形版本':'修改已保存，新地形版本已生成';$('#status').textContent=stateLabels[currentStatus];$('#binding').textContent=currentStatus==='terrain_edited'?'地形已修改；原导航失效，需重新检查后绑定场景。':'原地形检查已恢复';return true;
 }catch(e){$('#brushStatus').textContent='未修改：'+e.message;return false}finally{editing=false;$('#applyBrush').disabled=$('#undoTerrain').disabled=false}
}
$('#applyBrush').onclick=()=>submitTerrainEdit();$('#undoTerrain').onclick=()=>submitTerrainEdit(true);
window.terrainEditor={addPoint:addBrushPoint,get points(){return brushPoints},get tool(){return brushTool}};

function focusLake(){const water=data?.inlandWater?.[Number($('#lakeSelect').value)];if(!water)return;const c=new THREE.Vector3();let level=0;for(let i=0;i<water.positions.length;i+=3){const p=new THREE.Vector3(...water.positions.slice(i,i+3));c.add(p);level+=p.length()-R}c.normalize();level/=water.positions.length/3;focusPoint=c.clone().multiplyScalar(R+level+2);const tangent=new THREE.Vector3().crossVectors(Math.abs(c.y)>.9?new THREE.Vector3(1,0,0):new THREE.Vector3(0,1,0),c).normalize();const offset=c.clone().multiplyScalar(.75).addScaledVector(tangent,.65).normalize();azimuth=Math.atan2(offset.z,offset.x);polar=Math.acos(offset.y);const towns=(data.sceneInstances||[]).filter(s=>s.lakeIndex===Number($('#lakeSelect').value));const extent=Math.max(0,...towns.flatMap(s=>s.buildings.map(b=>Math.hypot(b.localPosition?.[0]||0,b.localPosition?.[2]||0)+3)));distance=extent?Math.max(22,extent*2.8):55;if(marker)marker.visible=false;}
$('#focusLake').onclick=focusLake;
$('#buildLakeScene').textContent='进入这个湖的场景能力管线';
$('#buildLakeScene').onclick=async()=>{if(!data||!$('#lakeSelect').options.length)return;if(data.inlandWater[Number($('#lakeSelect').value)]?.frame?.kind!=='local-tangent-plane'){if(!await submitTerrainEdit(false,{tool:'flatten_lakes'})){return}}const context={planet:id,terrain_version:version,lake:$('#lakeSelect').value,count:$('#lakeCount').value,floors:$('#lakeFloors').value,brief:$('#lakeBrief').value,model:$('#lakeUseModel').checked?'1':'0'};location.assign('/static/reacode.html?'+new URLSearchParams(context)+'#scene')};

$('#showGlobe').onclick=()=>{focusPoint=null;distance=R*3.2};

async function refreshPlanetAccount(){try{const status=await api('/api/auth/status');$('#planetAccount').textContent=status.user?'已登录本机账户：'+status.user:'尚未登录：请点击下方入口，在新标签页登录。';$('#planetLogin').hidden=Boolean(status.user)}catch(e){$('#planetAccount').textContent='无法读取登录状态，请打开控制台重试。';$('#planetLogin').hidden=false}}
window.addEventListener('focus',refreshPlanetAccount);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshPlanetAccount()});refreshPlanetAccount();

if(new URLSearchParams(location.search).has('embed')){document.documentElement.classList.add('planet-embed');const style=document.createElement('style');style.textContent='.planet-embed header,.planet-embed aside,.planet-embed .workflow,.planet-embed article>section{display:none}.planet-embed main{display:block;padding:0}.planet-embed #globe{height:100vh;min-height:0;border-radius:0}.planet-embed #hint{display:none}';document.head.append(style)}

$('#flattenLakes').onclick=async()=>{const ok=await submitTerrainEdit(false,{tool:'flatten_lakes'});$('#lakeSceneStatus').textContent=ok?'旧湖已转换为局部平面湖区，请进入场景管线重新生成建筑。':$('#brushStatus').textContent};

window.planetLakeEditor={highlight(buildingId){if(selectionOutline){scene.remove(selectionOutline);selectionOutline.geometry.dispose();selectionOutline.material.dispose();selectionOutline=null;}const town=(data.sceneInstances||[]).find(s=>s.id===lakeEditContext?.sceneId),building=town?.buildings.find(b=>b.id===buildingId);if(!building)return;const f=town.frame,[x,level,z]=building.localPosition,pts=[];const point=(a,b,h)=>f.origin.map((v,i)=>v+f.tangentU[i]*a+f.tangentV[i]*b+f.normal[i]*h);const corners=[[-1.8,-1.8],[1.8,-1.8],[1.8,1.8],[-1.8,1.8]];for(let i=0;i<4;i++){for(const h of [level+.03,level+building.floors*2.2+1])pts.push(...point(x+corners[i][0],z+corners[i][1],h),...point(x+corners[(i+1)%4][0],z+corners[(i+1)%4][1],h));pts.push(...point(x+corners[i][0],z+corners[i][1],level),...point(x+corners[i][0],z+corners[i][1],level+building.floors*2.2+1))}const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(pts,3));selectionOutline=new THREE.LineSegments(g,new THREE.LineBasicMaterial({color:0xffa31a,depthTest:false}));selectionOutline.renderOrder=20;scene.add(selectionOutline)},enable(sceneId){lakeEditContext={sceneId};host.style.cursor='crosshair'},disable(){lakeEditContext=null;host.style.cursor=''},async refresh(){loaded=null;await poll()},get context(){return lakeEditContext}};
const planetSceneChannel=new BroadcastChannel('genai-planet-scenes');planetSceneChannel.onmessage=event=>{if(event.data?.planet===id&&!editing&&!lakeEditContext){loaded=null;poll()}};
window.addEventListener('focus',()=>{if(data&&!editing){loaded=null;poll()}});
/* ---------------- Townscaper 式湖区建造 ---------------- */
/* 本地网格与 engines/system-workshop/planet/lake-town.js 完全一致：
   单元格 3.6×3.6，台基 3.6×3.6，楼体 4 边、每层 2.2m，屋顶 0.99m。 */
const LAKE_CELL=3.6,LAKE_LEVEL=.45,LAKE_FLOOR=2.2,LAKE_ROOF=.99,LAKE_MAX_FLOORS=5,LAKE_MAX_BLOCKS=64;
const LAKE_PALETTE=[[.85,.49,.35],[.89,.77,.48],[.45,.63,.67],[.61,.68,.48],[.67,.56,.7]],LAKE_STONE=[.61,.62,.57];
let lakeBlocks=[],lakeGhost=null,lakeGhostFill=null,lakeGhostEdge=null,lakeColorChoice=-1,lakeFloors=1,lakeStroke=null;
const lakeHighlightLegacy=window.planetLakeEditor.highlight.bind(window.planetLakeEditor);

const lakeSceneTown=()=>(data?.sceneInstances||[]).find(s=>s.id===lakeEditContext?.sceneId);
const lakeTownMesh=()=>townObjects.find(o=>o.userData.sceneId===lakeEditContext?.sceneId);
const lakeBlockAt=(x,z)=>lakeBlocks.find(b=>Math.abs(b.x-x)<.01&&Math.abs(b.z-z)<.01);
const lakeBlocksOf=scene=>(scene?.buildings||[]).map(b=>({id:b.id,x:b.localPosition[0],z:b.localPosition[2],floors:b.floors,color:b.color??0}));
const lakeIsPending=id=>typeof id==='string'&&id.indexOf('pending:')===0;

function lakeBasis(town){
 const f=town.frame;
 return {N:new THREE.Vector3(...f.normal),U:new THREE.Vector3(...f.tangentU),V:new THREE.Vector3(...f.tangentV),O:new THREE.Vector3(...f.origin)};
}
function lakePickColor(x,z){
 if(lakeColorChoice>=0)return lakeColorChoice;
 const i=Math.round(x/LAKE_CELL),j=Math.round(z/LAKE_CELL);
 let h=Math.imul((i*73856093)^(j*19349663),0x85ebca6b);
 h^=h>>>13;
 return Math.abs(h)%LAKE_PALETTE.length;
}
/* 与 storeyHouseModule(4,floors) 同构，保证本地预览和服务端结果一致。 */
function lakeStoreyTris(n,floors,seed){
 const inset=.76,roof=.45,P=(k,s=1)=>[Math.cos(k*Math.PI*2/n)*inset*s,Math.sin(k*Math.PI*2/n)*inset*s];
 const out=[],quad=(part,a,b,c,d)=>{
  out.push({part,v:[a,b,c]},{part,v:[a,c,d]});
 };
 const lerp2=(a,b,t)=>[a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t];
 for(let f=0;f<floors;f++)for(let k=0;k<n;k++){
  const a=P(k),b=P(k+1),h0=f,h1=f+1;
  if(f===0){
   quad('plinth',[...a,0],[...b,0],[...b,.14],[...a,.14]);
   quad('wall',[...a,.14],[...b,.14],[...b,h1],[...a,h1]);
  }else{
   quad('trim',[...a,h0],[...b,h0],[...b,h0+.07],[...a,h0+.07]);
   quad('wall',[...a,h0+.07],[...b,h0+.07],[...b,h1],[...a,h1]);
  }
  const A=P(k,1.012),B=P(k+1,1.012),door=f===0&&k===(seed%n);
  if(door)quad('door',[...lerp2(A,B,.38),.14],[...lerp2(A,B,.62),.14],[...lerp2(A,B,.62),.72],[...lerp2(A,B,.38),.72]);
  else quad('window',[...lerp2(A,B,.34),h0+.38],[...lerp2(A,B,.66),h0+.38],[...lerp2(A,B,.66),h0+.78],[...lerp2(A,B,.34),h0+.78]);
 }
 for(let k=0;k<n;k++){
  const a=P(k),b=P(k+1);
  out.push({part:'roof',v:[[...a,floors],[...b,floors],[0,0,floors+roof]]});
 }
 return out;
}
function lakeGeometry(town){
 const {N,U,V,O}=lakeBasis(town),P=[],C=[];
 const map=(x,z,h)=>[O.x+U.x*x+V.x*z+N.x*h,O.y+U.y*x+V.y*z+N.y*h,O.z+U.z*x+V.z*z+N.z*h];
 const tri=(points,color)=>{
  for(const p of points){
   P.push(p[0],p[1],p[2]);
   C.push(color[0],color[1],color[2]);
  }
 };
 const quad=(a,b,c,d,color)=>{
  tri([a,b,c],color);
  tri([a,c,d],color);
 };
 lakeBlocks.forEach((block,index)=>{
  const x=block.x,z=block.z,nf=Math.max(1,Math.min(LAKE_MAX_FLOORS,block.floors)),ci=Math.max(0,Math.min(LAKE_PALETTE.length-1,block.color??0));
  const ring=[[-1.8,-1.8],[1.8,-1.8],[1.8,1.8],[-1.8,1.8]].map(([a,c])=>[x+a,z+c]);
  const top=ring.map(([a,c])=>map(a,c,LAKE_LEVEL)),bottom=ring.map(([a,c])=>map(a,c,LAKE_LEVEL-2.5));
  quad(top[0],top[1],top[2],top[3],LAKE_STONE);
  for(let k=0;k<4;k++)quad(bottom[k],bottom[(k+1)%4],top[(k+1)%4],top[k],LAKE_STONE);
  for(const t of lakeStoreyTris(4,nf,index)){
   const color=t.part==='roof'?[.69,.27,.15]:t.part==='window'||t.part==='door'?[.16,.25,.29]:t.part==='plinth'||t.part==='trim'?LAKE_STONE:LAKE_PALETTE[ci];
   tri(t.v.map(([a,c,h])=>map(x+(a-c)*Math.SQRT1_2*2.25,z+(a+c)*Math.SQRT1_2*2.25,LAKE_LEVEL+h*2.2)),color);
  }
 });
 const g=new THREE.BufferGeometry();
 g.setAttribute('position',new THREE.Float32BufferAttribute(P,3));
 g.setAttribute('color',new THREE.Float32BufferAttribute(C,3));
 if(P.length)g.computeVertexNormals();
 return g;
}
function lakeSwapGeometry(geometry){
 const mesh=lakeTownMesh();
 if(!mesh||!geometry)return;
 mesh.geometry.dispose();
 mesh.geometry=geometry;
}
function lakeApplyBlocks(town){
 if(town?.frame)lakeSwapGeometry(lakeGeometry(town));
}
/* 服务端结果只替换这一片湖区的网格，不动相机、不整页重载。 */
function lakeApplyScene(scene){
 if(!scene||!data)return;
 const list=data.sceneInstances||(data.sceneInstances=[]);
 const index=list.findIndex(s=>s.id===scene.id);
 if(index>=0)list[index]=scene;
 else list.push(scene);
 if(lakeEditContext?.sceneId===scene.id){
  lakeBlocks=lakeBlocksOf(scene);
  const mesh=lakeTownMesh();
  if(mesh&&scene.mesh?.positions?.length){
   const g=new THREE.BufferGeometry();
   g.setAttribute('position',new THREE.Float32BufferAttribute(scene.mesh.positions,3));
   g.setAttribute('color',new THREE.Float32BufferAttribute(scene.mesh.colors,3));
   g.computeVertexNormals();
   lakeSwapGeometry(g);
  }else lakeApplyBlocks(scene);
  lakeHighlightLegacy(null);
 }
 lakeGhostHide();
}
function lakeCanBuild(x,z){
 const town=lakeSceneTown();
 if(!town)return false;
 const waterMesh=inlandObjects[town.lakeIndex];
 if(!waterMesh)return false;
 const {N,U,V,O}=lakeBasis(town);
 for(const [a,b]of [[0,0],[-1.7,-1.7],[1.7,-1.7],[1.7,1.7],[-1.7,1.7]]){
  const start=O.clone().addScaledVector(U,x+a).addScaledVector(V,z+b).addScaledVector(N,30);
  const probe=new THREE.Raycaster(start,N.clone().negate(),0,80);
  if(!probe.intersectObject(waterMesh,false).length)return false;
 }
 return true;
}
function lakeGhostBuild(){
 lakeGhost=new THREE.Group();
 lakeGhostFill=new THREE.Mesh(new THREE.BoxGeometry(LAKE_CELL-.08,1,LAKE_CELL-.08),new THREE.MeshBasicMaterial({color:0x2fbf71,transparent:true,opacity:.22,depthWrite:false}));
 lakeGhostEdge=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(LAKE_CELL,1,LAKE_CELL)),new THREE.LineBasicMaterial({color:0x1f9d5c,depthTest:false}));
 lakeGhostFill.renderOrder=28;
 lakeGhostEdge.renderOrder=29;
 lakeGhost.add(lakeGhostFill,lakeGhostEdge);
 lakeGhost.visible=false;
 scene.add(lakeGhost);
 return lakeGhost;
}
function lakeGhostHide(){
 if(lakeGhost)lakeGhost.visible=false;
}
/* 指针下的目标单元：点楼顶=叠层，点墙面=往该侧延伸，点水面=放置。 */
function lakeResolve(event,forceErase=false){
 const town=lakeSceneTown();
 if(!town?.frame)return null;
 const {N,U,V,O}=lakeBasis(town);
 const rect=renderer.domElement.getBoundingClientRect();
 const ray=new THREE.Raycaster();
 ray.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),camera);
 const erase=forceErase||event.button===2||event.shiftKey;
 const paint=!erase&&event.altKey;
 const mesh=lakeTownMesh(),hits=mesh?ray.intersectObject(mesh):[];
 if(hits.length){
  const hit=hits[0],local=hit.point.clone().sub(O),px=local.dot(U),pz=local.dot(V),ph=local.dot(N);
  let best=null,bestD=1e9;
  for(const block of lakeBlocks){
   const d=Math.hypot(block.x-px,block.z-pz);
   if(d<bestD){
    bestD=d;
    best=block;
   }
  }
  if(best&&bestD<=2.7){
   if(erase)return {
    kind:'erase',cell:{x:best.x,z:best.z},baseH:LAKE_LEVEL+(best.floors-1)*LAKE_FLOOR,height:LAKE_FLOOR,valid:true,
    intent:{action:'erase',building_id:best.id,x:best.x,z:best.z,floors:best.floors}
   };
   if(paint)return {
    kind:'paint',cell:{x:best.x,z:best.z},baseH:LAKE_LEVEL,height:best.floors*LAKE_FLOOR+LAKE_ROOF,valid:true,
    intent:{action:'paint',building_id:best.id,color:lakePickColor(best.x,best.z),x:best.x,z:best.z,floors:best.floors}
   };
   const normal=hit.face?.normal?hit.face.normal.clone():new THREE.Vector3(0,1,0);
   if(Math.abs(normal.dot(N))>.5){
    const floors=Math.min(LAKE_MAX_FLOORS,best.floors+1);
    return {
     kind:'stack',cell:{x:best.x,z:best.z},baseH:LAKE_LEVEL+best.floors*LAKE_FLOOR,height:LAKE_FLOOR+LAKE_ROOF,valid:best.floors<LAKE_MAX_FLOORS,
     intent:{action:'stack',building_id:best.id,floors,color:best.color,x:best.x,z:best.z}
    };
   }
   const du=normal.dot(U),dv=normal.dot(V),useU=Math.abs(du)>=Math.abs(dv);
   const x=best.x+(useU?Math.sign(du||1):0)*LAKE_CELL,z=best.z+(useU?0:Math.sign(dv||1))*LAKE_CELL;
   const levels=Math.max(1,Math.min(LAKE_MAX_FLOORS,Math.round((ph-LAKE_LEVEL)/LAKE_FLOOR)||1));
   const existing=lakeBlockAt(x,z);
   if(existing)return {
    kind:'stack',cell:{x,z},baseH:LAKE_LEVEL+existing.floors*LAKE_FLOOR,height:LAKE_FLOOR+LAKE_ROOF,valid:existing.floors<LAKE_MAX_FLOORS,
    intent:{action:'stack',building_id:existing.id,floors:Math.min(LAKE_MAX_FLOORS,existing.floors+1),color:existing.color,x,z}
   };
   return {
    kind:'extend',cell:{x,z},baseH:LAKE_LEVEL-.2,height:levels*LAKE_FLOOR+LAKE_ROOF,valid:lakeCanBuild(x,z)&&lakeBlocks.length<LAKE_MAX_BLOCKS,
    intent:{action:'extend',x,z,floors:levels,color:lakePickColor(x,z)}
   };
  }
 }
 const plane=new THREE.Plane().setFromNormalAndCoplanarPoint(N,O),point=new THREE.Vector3();
 if(!ray.ray.intersectPlane(plane,point))return null;
 const local=point.clone().sub(O),x=Math.round(local.dot(U)/LAKE_CELL)*LAKE_CELL,z=Math.round(local.dot(V)/LAKE_CELL)*LAKE_CELL;
 if(Math.abs(x)>100||Math.abs(z)>100)return null;
 const existing=lakeBlockAt(x,z);
 if(existing)return {
  kind:'stack',cell:{x,z},baseH:LAKE_LEVEL+existing.floors*LAKE_FLOOR,height:LAKE_FLOOR+LAKE_ROOF,valid:existing.floors<LAKE_MAX_FLOORS,
  intent:{action:'stack',building_id:existing.id,floors:Math.min(LAKE_MAX_FLOORS,existing.floors+1),color:existing.color,x,z}
 };
 const floors=Math.max(1,Math.min(LAKE_MAX_FLOORS,lakeFloors));
 return {
  kind:'place',cell:{x,z},baseH:LAKE_LEVEL-.2,height:floors*LAKE_FLOOR+LAKE_ROOF,valid:lakeCanBuild(x,z)&&lakeBlocks.length<LAKE_MAX_BLOCKS,
  intent:{action:'place',x,z,floors,color:lakePickColor(x,z)}
 };
}
function lakeHover(event){
 if(!lakeEditContext)return lakeGhostHide();
 const town=lakeSceneTown();
 if(!town?.frame)return lakeGhostHide();
 if(drag&&Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>8)return lakeGhostHide();
 const result=lakeResolve(event);
 if(!result)return lakeGhostHide();
 const {N,U,V,O}=lakeBasis(town),ghost=lakeGhost||lakeGhostBuild(),height=Math.max(.6,result.height);
 ghost.position.copy(O).addScaledVector(U,result.cell.x).addScaledVector(V,result.cell.z).addScaledVector(N,result.baseH+height/2);
 ghost.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(U,N,V));
 ghost.scale.set(1,height,1);
 ghost.visible=true;
 const tone=!result.valid?0xe05b5b:result.kind==='stack'?0xffa31a:result.kind==='erase'?0xe05b5b:result.kind==='paint'?0x7a6fff:0x2fbf71;
 lakeGhostFill.material.color.setHex(tone);
 lakeGhostEdge.material.color.setHex(tone);
}
function lakeSay(value){
 window.parent.postMessage({type:'genai-lake-notice',text:value},location.origin);
}
function lakeOptimistic(result){
 const intent=result.intent,town=lakeSceneTown();
 if(!town)return;
 if(intent.action==='erase'){
  const block=lakeBlockAt(intent.x,intent.z);
  if(!block)return;
  if(block.floors>1)block.floors-=1;
  else lakeBlocks=lakeBlocks.filter(item=>item!==block);
 }else if(intent.action==='stack'){
  const block=lakeBlockAt(intent.x,intent.z);
  if(!block)return;
  block.floors=Math.min(LAKE_MAX_FLOORS,intent.floors);
 }else if(intent.action==='paint'){
  const block=lakeBlockAt(intent.x,intent.z);
  if(!block)return;
  block.color=Math.max(0,Math.min(LAKE_PALETTE.length-1,intent.color));
 }else{
  lakeBlocks.push({id:'pending:'+intent.x+':'+intent.z,x:intent.x,z:intent.z,floors:intent.floors,color:intent.color});
 }
 lakeApplyBlocks(town);
}
/* 按住拖动时沿经过的格子连续建造 / 拆除：同样一格只处理一次。 */
function lakeStrokeStep(event,erase){
 if(!lakeEditContext)return;
 const result=lakeResolve(event,erase);
 if(!result)return;
 const key=result.cell.x.toFixed(1)+':'+result.cell.z.toFixed(1);
 if(lakeStroke.cells.has(key))return;
 lakeStroke.cells.add(key);
 lakeEditClick(event,erase);
}
function lakeEditClick(event,forceErase=false){
 if(!lakeEditContext)return;
 const town=lakeSceneTown();
 if(!town)return;
 const result=lakeResolve(event,forceErase);
 if(!result)return;
 if(!result.valid){
  lakeSay(result.kind==='stack'?'这栋已经到 5 层上限。':'这里不在有效湖面上，台基没有湖底承托；换个位置再点。');
  return;
 }
 if(result.kind!=='stack'&&result.kind!=='erase'&&result.kind!=='paint'&&lakeBlocks.length>=LAKE_MAX_BLOCKS){
  lakeSay('这个湖区最多 64 栋，先拆掉一些再建。');
  return;
 }
 if(lakeIsPending(result.intent.building_id)){
  lakeSay('上一块还在保存，稍等一下再点这一栋。');
  return;
 }
 lakeOptimistic(result);
 window.parent.postMessage({
  type:'genai-lake-edit-intent',planet:id,terrainVersion:version,sceneId:town.id,...result.intent
 },location.origin);
}
Object.assign(window.planetLakeEditor,{
 enable(sceneId,options={}){
  lakeEditContext={sceneId};
  lakeColorChoice=Number.isInteger(options.color)?options.color:-1;
  lakeFloors=Number.isInteger(options.floors)?options.floors:lakeFloors;
  const town=lakeSceneTown();
  lakeBlocks=lakeBlocksOf(town);
  host.style.cursor='crosshair';
  lakeGhostHide();
  return Boolean(town?.frame);
 },
 disable(){
  lakeEditContext=null;
  host.style.cursor='';
  lakeGhostHide();
  lakeHighlightLegacy(null);
 },
 setColor(index){
  lakeColorChoice=Number.isInteger(index)?index:-1;
 },
 setFloors(count){
  lakeFloors=Math.max(1,Math.min(LAKE_MAX_FLOORS,Number(count)||1));
 },
 highlight(buildingId){
  lakeHighlightLegacy(buildingId);
 },
 applyScene(scene){
  lakeApplyScene(scene);
 },
 async refresh(){
  loaded=null;
  await poll();
 },
 get context(){
  return lakeEditContext;
 }
});
Object.defineProperty(window.planetLakeEditor,'floors',{
 get(){
  return lakeFloors;
 }
});
Object.defineProperty(window.planetLakeEditor,'blocks',{
 get(){
  return lakeBlocks.map(block=>({
   ...block
  }));
 }
});

renderer.domElement.addEventListener('pointerleave',()=>lakeGhostHide());
renderer.domElement.addEventListener('contextmenu',event=>{
 if(lakeEditContext)event.preventDefault();
});
