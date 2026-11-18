import * as THREE from 'three';
const $=s=>document.querySelector(s),host=$('#globe');let id=new URLSearchParams(location.search).get('run'),data=null,version=null,selected=null,loaded=null,currentStatus=null,pollEpoch=0,pollTimer=null,creating=false;
const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));host.prepend(renderer.domElement);renderer.setClearColor('#e6f2f5');const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(40,1,.1,5000);scene.add(new THREE.HemisphereLight(0xffffff,0x64777c,2));const sun=new THREE.DirectionalLight(0xffffff,2);sun.position.set(3,5,4);scene.add(sun);let surface,ocean,wire,marker;let inlandObjects=[],townObjects=[],brushTool='select',brushPoints=[],brushMarkers=[],editing=false;let azimuth=.8,polar=1.05,distance=600,R=160,focusPoint=null;let selectionOutline=null;
function resize(){renderer.setSize(host.clientWidth,host.clientHeight,false);camera.aspect=host.clientWidth/host.clientHeight;camera.updateProjectionMatrix()}new ResizeObserver(resize).observe(host);
function animate(){requestAnimationFrame(animate);camera.position.set(distance*Math.sin(polar)*Math.cos(azimuth),distance*Math.cos(polar),distance*Math.sin(polar)*Math.sin(azimuth));if(focusPoint){camera.position.add(focusPoint);camera.lookAt(focusPoint)}else camera.lookAt(0,0,0);renderer.render(scene,camera);if(window.planetPreview?.ready)window.planetPreview.frameRendered=true}animate();
let drag=null;
const lakeBuilding=()=>Boolean(lakeBuildMode&&lakeInsitu);
renderer.domElement.onpointerdown=e=>{
 if(lakeBuilding()){
  if(e.button===2){
   drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,mode:'right'};
   renderer.domElement.setPointerCapture(e.pointerId);
   return;
  }
  if(e.button===1){
   drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,mode:'orbit'};
   renderer.domElement.setPointerCapture(e.pointerId);
   return;
  }
  if(e.button===0){
   if(e.altKey){
    lakeBuildAt(e,{paint:true});
    renderer.domElement.setPointerCapture(e.pointerId);
    return;
   }
   lakeStroke={snap:lakeInsitu.world.snapshot(),cells:new Set(),changed:false};
   lakeBuildAt(e,{erase:e.shiftKey});
   renderer.domElement.setPointerCapture(e.pointerId);
   return;
  }
  return;
 }
 drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY};renderer.domElement.setPointerCapture(e.pointerId)};
renderer.domElement.onpointermove=e=>{
 if(lakeBuilding()){
  if(lakeStroke){
   if(e.buttons&1)lakeStrokeStep(e,e.shiftKey);
   return;
  }
  if(drag){
   if(drag.mode==='right'&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>6)drag.mode='orbit';
   if(drag.mode==='orbit'){
    azimuth-=(e.clientX-drag.x)*.006;
    polar=THREE.MathUtils.clamp(polar+(e.clientY-drag.y)*.006,.07,Math.PI-.07);
    drag.x=e.clientX;drag.y=e.clientY;
   }
  }
  return;
 }
 if(!drag)return;azimuth-=(e.clientX-drag.x)*.006;polar=THREE.MathUtils.clamp(polar+(e.clientY-drag.y)*.006,.07,Math.PI-.07);drag.x=e.clientX;drag.y=e.clientY};
renderer.domElement.onpointerup=e=>{
 if(lakeBuilding()){
  if(lakeStroke){
   lakeEndStroke();
   return;
  }
  if(drag){
   const rightClick=drag.mode==='right'&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)<=6;
   drag=null;
   if(rightClick)lakeBuildAt(e,{erase:true});
  }
  return;
 }
 if(drag&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)<8&&surface){const rect=renderer.domElement.getBoundingClientRect(),ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);const hit=ray.intersectObject(surface)[0];if(hit){const d=hit.point.normalize();if(brushTool!=='select'){addBrushPoint(d.toArray());drag=null;return}const region=data.regions.reduce((a,b)=>new THREE.Vector3(...a.direction).dot(d)>new THREE.Vector3(...b.direction).dot(d)?a:b);selectRegion(region.id)}}drag=null};
renderer.domElement.onwheel=e=>{e.preventDefault();distance=THREE.MathUtils.clamp(distance*Math.exp(e.deltaY*.001),focusPoint?10:R*1.3,focusPoint?R*3:R*8)};
renderer.domElement.addEventListener('wheel',e=>e.preventDefault(),{passive:false});
function selectRegion(regionId){selected=data.regions.find(r=>r.id===regionId);if(!selected)return;$('#regionSelect').value=selected.id;$('#region').replaceChildren();for(const text of ['区域 '+selected.id,selected.land?'陆地':'海域','地表高度 '+selected.height.toFixed(2),'邻接 '+selected.neighbors.join('、'),'场景实例：'+(data.sceneInstances||[]).filter(s=>data.regions.reduce((a,b)=>new THREE.Vector3(...a.direction).dot(new THREE.Vector3(...s.center))>new THREE.Vector3(...b.direction).dot(new THREE.Vector3(...s.center))?a:b).id===selected.id).length]){const p=document.createElement('p');p.textContent=text;$('#region').append(p)}if(marker){scene.remove(marker);marker.geometry.dispose();marker.material.dispose()}marker=new THREE.Mesh(new THREE.SphereGeometry(R*.018,12,8),new THREE.MeshBasicMaterial({color:0x087a89,depthTest:false}));marker.renderOrder=10;marker.visible=!new URLSearchParams(location.search).has('embed');marker.position.fromArray(selected.direction).multiplyScalar(R+selected.height+R*.02);scene.add(marker);$('#reserve').disabled=!version||data.checks.closed!==true||currentStatus!=='terrain_checked';window.planetPreview.selected=selected}
function load(payload){if(selectionOutline){scene.remove(selectionOutline);selectionOutline.geometry.dispose();selectionOutline.material.dispose();selectionOutline=null;}for(const o of inlandObjects){scene.remove(o);o.geometry.dispose();o.material.dispose()}inlandObjects=[];for(const o of townObjects){scene.remove(o);o.geometry.dispose();o.material.dispose()}townObjects=[];data=payload.planet;version=payload.state.version;R=data.recipe.radius;distance=R*3.2;focusPoint=null;for(const o of [surface,ocean,wire,marker])if(o){scene.remove(o);o.geometry.dispose();o.material.dispose()}marker=null;const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(data.mesh.positions,3));g.setAttribute('color',new THREE.Float32BufferAttribute(data.mesh.colors,3));g.setIndex(data.mesh.indices);g.computeVertexNormals();surface=new THREE.Mesh(g,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1}));scene.add(surface);ocean=new THREE.Mesh(new THREE.SphereGeometry(R+data.recipe.seaLevel+.08,128,64),new THREE.MeshStandardMaterial({color:0x74b3c5,roughness:.5}));scene.add(ocean);wire=new THREE.LineSegments(new THREE.WireframeGeometry(g),new THREE.LineBasicMaterial({color:0x386875,transparent:true,opacity:.25}));wire.visible=$('#wire').checked;scene.add(wire);for(const w of data.inlandWater||[]){const wg=new THREE.BufferGeometry();wg.setAttribute('position',new THREE.Float32BufferAttribute(w.positions,3));wg.setIndex(w.indices);wg.computeVertexNormals();const wm=new THREE.Mesh(wg,new THREE.MeshStandardMaterial({color:0x62acbf,roughness:.5,side:THREE.DoubleSide}));scene.add(wm);inlandObjects.push(wm)}$('#regionSelect').replaceChildren();for(const r of data.regions){const o=document.createElement('option');o.value=r.id;o.textContent=r.id+' · '+(r.land?'陆地':'海域');$('#regionSelect').append(o)}$('#stats').replaceChildren();const rows=[['半径',R],['顶点',data.stats.vertices],['三角面',data.stats.triangles],['全球区域',data.stats.regions],['采样陆地点占比',(100*data.stats.sampledLandRatio).toFixed(1)+'%'],['高度范围',data.stats.minHeight.toFixed(2)+'–'+data.stats.maxHeight.toFixed(2)],['导航图节点',data.navigation.nodes],['湖区场景',(data.sceneInstances||[]).length],['湖区建筑',(data.sceneInstances||[]).reduce((n,s)=>n+s.buildings.length,0)]];for(const [a,b]of rows){const el=document.createElement('div');el.textContent=a+'：'+b;$('#stats').append(el)}$('#checks').replaceChildren();for(const [a,b]of [['编译器',data.checks.compiler],['完整采样球面闭合',data.checks.closed],['有限坐标',data.checks.finite],['Euler = 2',data.checks.euler===2],...Object.entries(data.checks.reports).map(([k,v])=>[k,v.ok])]){const el=document.createElement('div');el.textContent=(b?'✓ ':'✕ ')+a;$('#checks').append(el)}$('#version').textContent=version||'等待版本';$('#hint').textContent='整颗星球 · 拖动旋转 · 点击选区';
for(const town of data.sceneInstances||[]){const tg=town.kind==='lake-world'?lakeWorldGeometry(town):new THREE.BufferGeometry();if(town.kind!=='lake-world'){tg.setAttribute('position',new THREE.Float32BufferAttribute(town.mesh.positions,3));tg.setAttribute('color',new THREE.Float32BufferAttribute(town.mesh.colors,3));tg.computeVertexNormals()}const tm=new THREE.Mesh(tg,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1,side:THREE.DoubleSide}));tm.userData.sceneId=town.id;scene.add(tm);townObjects.push(tm)}
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

function focusLake(){const water=data?.inlandWater?.[Number($('#lakeSelect').value)];if(!water)return;const c=new THREE.Vector3();let level=0;for(let i=0;i<water.positions.length;i+=3){const p=new THREE.Vector3(...water.positions.slice(i,i+3));c.add(p);level+=p.length()-R}c.normalize();level/=water.positions.length/3;focusPoint=c.clone().multiplyScalar(R+level+2);const tangent=new THREE.Vector3().crossVectors(Math.abs(c.y)>.9?new THREE.Vector3(1,0,0):new THREE.Vector3(0,1,0),c).normalize();const offset=c.clone().normalize();azimuth=Math.atan2(offset.z,offset.x);polar=Math.acos(offset.y);const towns=(data.sceneInstances||[]).filter(s=>s.lakeIndex===Number($('#lakeSelect').value));
// 有体素场景时按楼群自己的中心取景（楼群常常偏在湖的一角，按湖心会拍空）
const lw=towns.find(s=>s.kind==='lake-world'&&s.center&&s.extent);
if(lw){
 const dir=new THREE.Vector3(...lw.center).normalize();
 focusPoint=dir.clone().multiplyScalar(R+4);
 const t2=new THREE.Vector3().crossVectors(Math.abs(dir.y)>.9?new THREE.Vector3(1,0,0):new THREE.Vector3(0,1,0),dir).normalize();
 const off=dir.clone().multiplyScalar(.75).addScaledVector(t2,.65).normalize();
 azimuth=Math.atan2(off.z,off.x);polar=Math.acos(off.y);
 distance=Math.max(42,Math.max(lw.extent.width||0,lw.extent.depth||0)*1.8);
}else{
 let extent=0;
 for(const s of towns){if(s.kind==='lake-world'&&s.mesh?.positions){const p=s.mesh.positions;for(let i=0;i<p.length;i+=3)extent=Math.max(extent,Math.hypot(p[i],p[i+2]))}else for(const b of s.buildings||[])extent=Math.max(extent,Math.hypot(b.localPosition?.[0]||0,b.localPosition?.[2]||0)+3)}
 distance=extent?Math.max(45,extent*2.2):60;
}
if(marker)marker.visible=false;}
$('#focusLake').onclick=focusLake;
$('#buildLakeScene').textContent='进入这个湖的场景能力管线';
$('#buildLakeScene').onclick=async()=>{if(!data||!$('#lakeSelect').options.length)return;if(data.inlandWater[Number($('#lakeSelect').value)]?.frame?.kind!=='local-tangent-plane'){if(!await submitTerrainEdit(false,{tool:'flatten_lakes'})){return}}const context={planet:id,terrain_version:version,lake:$('#lakeSelect').value,count:$('#lakeCount').value,floors:$('#lakeFloors').value,brief:$('#lakeBrief').value,model:$('#lakeUseModel').checked?'1':'0'};location.assign('/static/reacode.html?'+new URLSearchParams(context)+'#scene')};

$('#showGlobe').onclick=()=>{focusPoint=null;distance=R*3.2};

async function refreshPlanetAccount(){try{const status=await api('/api/auth/status');$('#planetAccount').textContent=status.user?'已登录本机账户：'+status.user:'尚未登录：请点击下方入口，在新标签页登录。';$('#planetLogin').hidden=Boolean(status.user)}catch(e){$('#planetAccount').textContent='无法读取登录状态，请打开控制台重试。';$('#planetLogin').hidden=false}}
window.addEventListener('focus',refreshPlanetAccount);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshPlanetAccount()});refreshPlanetAccount();

if(new URLSearchParams(location.search).has('embed')){document.documentElement.classList.add('planet-embed');const style=document.createElement('style');style.textContent='.planet-embed header,.planet-embed aside,.planet-embed .workflow,.planet-embed article>section{display:none}.planet-embed main{display:block;padding:0}.planet-embed #globe{height:100vh;min-height:0;border-radius:0}.planet-embed #hint{display:none}';document.head.append(style)}

$('#flattenLakes').onclick=async()=>{const ok=await submitTerrainEdit(false,{tool:'flatten_lakes'});$('#lakeSceneStatus').textContent=ok?'旧湖已转换为局部平面湖区，请进入场景管线重新生成建筑。':$('#brushStatus').textContent};

const planetSceneChannel=new BroadcastChannel('genai-planet-scenes');
planetSceneChannel.onmessage=event=>{if(event.data?.planet===id&&!editing&&!lakeBuildMode){loaded=null;poll()}};
window.addEventListener('focus',()=>{if(data&&!editing&&!lakeBuildMode){loaded=null;poll()}});

/* ---------------- 球面就地建造：球与场景一体 ---------------- */
/* 数据是 REACodeRefactoring 的 World 体素场，出图是 build/buildings.js，
   球面只负责把本地坐标按湖面 frame 贴回球壳 —— 没有第二套房子生成器。 */
let lakeBuildMode=false,lakeInsitu=null,lakeRefScene=null,lakeColorChoice=-1,lakeFloors=2,lakeStroke=null;
let lakeDirty=false,lakeSaveTimer=null,lakeSaveState='idle',lakeRebuildPending=false,lakeSaving=false;
const LAKE_COLORS=['#dd6a5f','#eec463','#85b780','#6f9dc6','#ac91c9','#e8a377','#c2ab8b','#ecdfc4','#8b9fa6','#cf5348','#dd9057','#d3b96b','#63a291','#9dbdd7','#cf9ab5'];

function lakeBasis(town){
 const f=town.frame;
 return {N:new THREE.Vector3(...f.normal),U:new THREE.Vector3(...f.tangentU),V:new THREE.Vector3(...f.tangentV),O:new THREE.Vector3(...f.origin)};
}
const lakeTownMesh=()=>townObjects.find(o=>o.userData.sceneId===lakeRefScene?.id);
function lakeSwapGeometry(geometry){
 const mesh=lakeTownMesh();
 if(!mesh||!geometry)return;
 mesh.geometry.dispose();
 mesh.geometry=geometry;
}
/* 服务端已烘好的 lake-world 网格：本地 x / y(高度) / z → 球面 frame。 */
function lakeWorldGeometry(town){
 const {N,U,V,O}=lakeBasis(town),p=town.mesh.positions,c=town.mesh.colors||[],P=new Array(p.length),C=new Array(p.length);
 for(let i=0;i<p.length;i+=3){
  const x=p[i],y=p[i+1],z=p[i+2];
  P[i]=O.x+U.x*x+V.x*z+N.x*y;
  P[i+1]=O.y+U.y*x+V.y*z+N.y*y;
  P[i+2]=O.z+U.z*x+V.z*z+N.z*y;
  C[i]=c[i]??.7;C[i+1]=c[i+1]??.7;C[i+2]=c[i+2]??.7;
 }
 const g=new THREE.BufferGeometry();
 g.setAttribute('position',new THREE.Float32BufferAttribute(P,3));
 g.setAttribute('color',new THREE.Float32BufferAttribute(C,3));
 g.computeVertexNormals();
 return g;
}

function lakeStatus(text){
 const el=document.querySelector('#hint');
 if(el)el.textContent=text;
}
function lakeColorIndex(cell){
 if(lakeColorChoice>=0)return lakeColorChoice;
 const i=Math.round((cell?.cx||0)/0.5),j=Math.round((cell?.cz||0)/0.5);
 let h=Math.imul((i*73856093)^(j*19349663),0x85ebca6b);
 h^=h>>>13;
 return Math.abs(h)%LAKE_COLORS.length;
}
/* 球面点击 → 球壳上的点：先打建筑网格与这块湖的水面，都没有就打湖面切平面。 */
function lakePointerPoint(event){
 const rect=renderer.domElement.getBoundingClientRect();
 const ray=new THREE.Raycaster();
 ray.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),camera);
 const targets=[];
 const mesh=lakeTownMesh();
 if(mesh)targets.push(mesh);
 const water=inlandObjects[lakeRefScene?.lakeIndex];
 if(water)targets.push(water);
 const hits=targets.length?ray.intersectObjects(targets,false):[];
 if(hits.length)return hits[0].point.clone();
 const f=lakeInsitu?.frame;
 if(!f)return null;
 const plane=new THREE.Plane().setFromNormalAndCoplanarPoint(f.N,f.O);
 const point=new THREE.Vector3();
 return ray.ray.intersectPlane(plane,point)?point:null;
}
function lakeBuildAt(event,{erase=false,paint=false}={},hit=null){
 if(!lakeBuildMode||!lakeInsitu||!lakeRefScene)return false;
 if(!hit){
  const point=lakePointerPoint(event);
  if(!point)return false;
  hit=lakeInsitu.resolve(point,{erase,paint,colorIndex:0,floors:lakeFloors});
 }
 if(!hit)return false;
 const color=lakeColorIndex(hit.cell);
 if(!hit.wet&&hit.top===0&&hit.tool!=='erase'){
  lakeStatus('这里是湖岸岩体，点水面才会长楼');
  return false;
 }
 if(!lakeInsitu.apply(hit.cell,hit.level,hit.tool,color))return false;
 if(lakeStroke)lakeStroke.changed=true;
 lakeDirty=true;
 lakeRebuildSoon();
 lakeQueueSave();
 return true;
}
/* 按住拖动：沿经过的格连续建造，同一格只处理一次。 */
function lakeStrokeStep(event,erase=false){
 if(!lakeStroke||!lakeInsitu)return;
 const point=lakePointerPoint(event);
 if(!point)return;
 const hit=lakeInsitu.pick(point);
 if(!hit||lakeStroke.cells.has(hit.cell.id))return;
 lakeStroke.cells.add(hit.cell.id);
 lakeBuildAt(event,{erase},hit);
}
function lakeEndStroke(){
 if(!lakeStroke)return;
 const stroke=lakeStroke;
 lakeStroke=null;
 if(stroke.changed)lakeInsitu?.pushUndo(stroke.snap);
}
function lakeRebuild(){
 if(!lakeInsitu||!lakeRefScene)return;
 lakeSwapGeometry(lakeInsitu.buildGeometry());
 lakePaintBar();
}
function lakeRebuildSoon(){
 if(lakeRebuildPending)return;
 lakeRebuildPending=true;
 requestAnimationFrame(()=>{lakeRebuildPending=false;lakeRebuild()});
}
function lakeStats(){
 return lakeInsitu?lakeInsitu.stats():{buildings:0,blocks:0,floors:0};
}
function lakePaintBar(){
 const bar=document.querySelector('#lakeBuildBar');
 if(!bar)return;
 bar.hidden=!lakeBuildMode;
 if(!lakeBuildMode)return;
 const stats=lakeStats();
 const count=document.querySelector('#lakeBuildStats');
 if(count)count.textContent=stats.buildings+' 栋 / '+stats.blocks+' 块';
 const save=document.querySelector('#lakeBuildSave');
 if(save){
  save.textContent=lakeSaveState==='saving'?'保存中':lakeSaveState==='saved'?'已存':lakeSaveState==='error'?'未存':'待存';
  save.dataset.state=lakeSaveState;
 }
 const undo=document.querySelector('#lakeBuildUndo');
 if(undo)undo.disabled=!lakeInsitu?.canUndo();
 const floors=document.querySelector('#lakeBuildFloors');
 if(floors)floors.value=String(lakeFloors);
 const colors=document.querySelector('#lakeBuildColors');
 if(colors)for(const [i,el] of [...colors.children].entries())el.classList.toggle('on',i===lakeColorChoice);
}
function lakeBuildColorButtons(){
 const hostEl=document.querySelector('#lakeBuildColors');
 if(!hostEl||hostEl.children.length)return;
 LAKE_COLORS.forEach((hex,i)=>{
  const button=document.createElement('button');
  button.type='button';
  button.style.background=hex;
  button.title='第 '+(i+1)+' 色';
  button.onclick=()=>{lakeColorChoice=lakeColorChoice===i?-1:i;lakePaintBar()};
  hostEl.append(button);
 });
}
/* ---- 自动保存：停手 1.2 s 写回球系统，离开页面再兜一次 ------------------ */
function lakeQueueSave(){
 if(!lakeBuildMode)return;
 if(lakeSaveTimer)clearTimeout(lakeSaveTimer);
 lakeSaveState='dirty';
 lakePaintBar();
 lakeSaveTimer=setTimeout(()=>{lakeSaveTimer=null;lakeSave()},1200);
}
function lakeSavePayload(){
 return {state:lakeInsitu.exportState({lakeIndex:lakeRefScene.lakeIndex,terrainVersion:version}),stats:lakeInsitu.stats(),sceneId:lakeRefScene.id};
}
async function lakeSave(){
 if(!lakeBuildMode||!lakeInsitu||!lakeRefScene||lakeSaving)return false;
 lakeSaving=true;
 lakeSaveState='saving';lakePaintBar();
 if(lakeSaveTimer){clearTimeout(lakeSaveTimer);lakeSaveTimer=null}
 try{
  const payload=lakeSavePayload();
  const response=await fetch('/api/reacode/planet-lake-world/'+encodeURIComponent(id)+'/'+lakeRefScene.lakeIndex,{
   method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)
  });
  const out=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(out.detail||('HTTP '+response.status));
  const scene=out.scene;
  if(scene&&data){
   const list=data.sceneInstances||(data.sceneInstances=[]);
   const index=list.findIndex(item=>item.id===scene.id);
   if(index>=0)list[index]={...list[index],...scene};else list.push(scene);
   lakeRefScene=list.find(item=>item.id===scene.id)||scene;
  }
  lakeDirty=false;lakeSaveState='saved';
  window.parent.postMessage({type:'genai-lake-saved',buildings:lakeInsitu.stats().buildings,lakeIndex:lakeRefScene.lakeIndex},location.origin);
  return true;
 }catch(err){
  lakeSaveState='error';
  lakeStatus('保存失败：'+err.message);
  return false;
 }finally{
  lakeSaving=false;lakePaintBar();
 }
}
window.addEventListener('pagehide',()=>{
 if(!lakeDirty||!lakeBuildMode||!lakeInsitu||!lakeRefScene)return;
 try{
  const blob=new Blob([JSON.stringify(lakeSavePayload())],{type:'application/json'});
  navigator.sendBeacon('/api/reacode/planet-lake-world/'+encodeURIComponent(id)+'/'+lakeRefScene.lakeIndex,blob);
 }catch{/* 离开时不再提示 */}
});

/* ---- 进入 / 退出 ------------------------------------------------------- */
async function lakeEnter(sceneId){
 if(lakeBuildMode)return true;
 if(!data)return false;
 const town=(data.sceneInstances||[]).find(s=>s.id===sceneId)
  ||(data.sceneInstances||[]).find(s=>s.kind==='lake-world'&&s.lakeIndex===Number(document.querySelector('#lakeSelect').value));
 if(!town){lakeStatus('这个湖区还没有可建造的场景，请先生成并采用候选');return false}
 const water=(data.inlandWater||[])[town.lakeIndex];
 if(!water?.frame){lakeStatus('这个湖区还是旧版曲面，请先转成平面湖区');return false}
 try{
  const {createLakeInsitu}=await import('/static/lake-insitu.js?v=insitu-1');
  lakeInsitu=createLakeInsitu({water,scene:town});
 }catch(err){
  lakeStatus('建造模块加载失败：'+err.message);
  return false;
 }
 if(lakeInsitu.restoreError)console.warn('湖区体素存档未能读回，已按湖面重建',lakeInsitu.restoreError);
 lakeRefScene=town;
 lakeBuildMode=true;
 document.body.classList.add('lake-build-mode');
 lakeDirty=false;lakeSaveState='idle';
 host.style.cursor='crosshair';
 lakeBuildColorButtons();lakePaintBar();
 lakeRebuild();
 lakeStatus('球面建造 · 左键拖动长楼，点楼顶叠层，Shift/右键点削层，Alt 涂色，右键拖动转视角');
 return true;
}
function lakeExit(){
 if(!lakeBuildMode)return;
 const dirty=lakeDirty&&Boolean(lakeInsitu&&lakeRefScene);
 if(dirty){
  // 退出前补一次保存，用 keepalive 保证请求不被页面导航打断
  try{
   fetch('/api/reacode/planet-lake-world/'+encodeURIComponent(id)+'/'+lakeRefScene.lakeIndex,{
    method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(lakeSavePayload()),keepalive:true
   }).catch(()=>{});
  }catch{/* 忽略 */}
 }
 lakeBuildMode=false;
 document.body.classList.remove('lake-build-mode');
 lakeStroke=null;
 host.style.cursor='';
 if(lakeSaveTimer){clearTimeout(lakeSaveTimer);lakeSaveTimer=null}
 lakeInsitu=null;lakeRefScene=null;
 lakePaintBar();
 lakeStatus('整颗星球 · 拖动旋转 · 点击选区');
}

window.planetLakeEditor={
 get active(){return lakeBuildMode},
 get context(){return lakeRefScene?{sceneId:lakeRefScene.id,lakeIndex:lakeRefScene.lakeIndex}:null},
 get floors(){return lakeFloors},
 get blocks(){return lakeInsitu?lakeInsitu.stats().blocks:0},
 async enter(sceneId){return lakeEnter(sceneId)},
 exit(){lakeExit()},
 save(){return lakeSave()},
 undo(){
  if(!lakeInsitu?.undo())return false;
  lakeDirty=true;lakeRebuild();lakeQueueSave();return true;
 },
 redo(){
  if(!lakeInsitu?.redo())return false;
  lakeDirty=true;lakeRebuild();lakeQueueSave();return true;
 },
 setColor(index){lakeColorChoice=Number.isInteger(index)?index:-1;lakePaintBar()},
 setFloors(count){lakeFloors=Math.max(1,Math.min(5,Number(count)||1));lakePaintBar()},
 highlight(){},
 applyScene(scene){
  if(!scene||!data)return;
  const list=data.sceneInstances||(data.sceneInstances=[]);
  const index=list.findIndex(item=>item.id===scene.id);
  if(index>=0)list[index]=scene;else list.push(scene);
  if(lakeRefScene?.id===scene.id)lakeRefScene=scene;
 },
 topOf(cellId){
  if(!lakeInsitu)return 0;
  const cell=lakeInsitu.world.cells[cellId];
  return cell?lakeInsitu.world.top(cell):0;
 },
 debugAt(clientX,clientY){
  if(!lakeInsitu)return null;
  const point=lakePointerPoint({clientX,clientY});
  if(!point)return null;
  const hit=lakeInsitu.resolve(point,{colorIndex:0,floors:lakeFloors});
  return hit?{cellId:hit.cell.id,level:hit.level,top:hit.top,wet:hit.wet,tool:hit.tool,dist:hit.dist,local:[+hit.local.x.toFixed(2),+hit.local.y.toFixed(2),+hit.local.z.toFixed(2)],floors:lakeFloors}:null;
 },
 probe(clientX,clientY){
  if(!lakeInsitu)return null;
  const point=lakePointerPoint({clientX,clientY});
  if(!point)return null;
  const hit=lakeInsitu.pick(point);
  return hit?{cellId:hit.cell.id,level:hit.level,top:hit.top,wet:hit.wet,dist:hit.dist}:null;
 },
 async refresh(){loaded=null;await poll()}
};

window.addEventListener('message',event=>{
 if(event.origin!==location.origin)return;
 const value=event.data;
 if(value?.type==='genai-lake-build')value.on?lakeEnter(value.sceneId):lakeExit();
 if(value?.type==='genai-lake-command'&&lakeBuildMode){
  if(value.command==='undo')window.planetLakeEditor.undo();
  else if(value.command==='redo')window.planetLakeEditor.redo();
  else if(value.command==='save')lakeSave();
  else if(value.command==='exit')lakeExit();
 }
});
window.addEventListener('keydown',event=>{
 if(!lakeBuildMode)return;
 if(event.key==='Escape'){lakeExit();return}
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){
  event.preventDefault();
  event.shiftKey?window.planetLakeEditor.redo():window.planetLakeEditor.undo();
 }
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();lakeSave()}
});
const lakeFloorsInput=document.querySelector('#lakeBuildFloors');
if(lakeFloorsInput)lakeFloorsInput.onchange=event=>{lakeFloors=Math.max(1,Math.min(5,Number(event.target.value)||1))};
const lakeUndoButton=document.querySelector('#lakeBuildUndo');
if(lakeUndoButton)lakeUndoButton.onclick=()=>window.planetLakeEditor.undo();
const lakeExitButton=document.querySelector('#lakeBuildExit');
if(lakeExitButton)lakeExitButton.onclick=()=>lakeExit();

renderer.domElement.addEventListener('contextmenu',event=>{
 if(lakeBuildMode)event.preventDefault();
});
