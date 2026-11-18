const params=new URLSearchParams(location.search);
if(params.has('planet'))await import('./rea-planet-workshop.js?v=townscaper-10');
else if(params.get('demo')==='1'||params.has('run')){
 await import('./rea-workshop.js?v=361d8b07c68f');
 const label=document.querySelector('#sceneSubtitle');if(label)label.textContent='独立示例场景 · 未连接星球湖区；不会修改球系统。';
}else{
 let context=null;
 try{context=JSON.parse(localStorage.getItem('genai.rea.active-scene-context'))}catch{}
 if(!context){try{const latest=await (await fetch('/api/reacode/planet-latest',{cache:'no-store'})).json();if(latest.id){const payload=await (await fetch('/api/reacode/planet-data/'+encodeURIComponent(latest.id),{cache:'no-store'})).json();const lake=(payload.planet?.inlandWater||[]).findIndex(w=>w.kind==='lake');if(lake>=0)context={planet:latest.id,terrain_version:payload.state.version,lake}}}catch{}}
 if(context?.planet&&context?.terrain_version&&Number.isInteger(Number(context.lake))){location.replace('/static/reacode.html?'+new URLSearchParams(context)+'#scene')}
 else{document.querySelector('#connection').textContent='尚未选择星球湖区';document.querySelector('#viewerOverlay').textContent='先从球系统选择湖泊进入场景编辑；不会自动加载独立示例。';document.querySelector('#sceneSubtitle').textContent='尚未绑定星球。请先选择湖区，或明确打开独立示例。';const holder=document.querySelector('#pipeline');for(const [text,url]of [['打开球系统选择湖泊','/static/planet-studio.html'],['打开独立示例（与星球无关）','/static/reacode.html?demo=1']]){const a=document.createElement('a');a.textContent=text;a.href=url;a.className='secondary';holder.append(a)}for(const id of ['createScene','generate','scan','edit','play','saveRecipe'])document.querySelector('#'+id).disabled=true;document.querySelector('#sceneSelect').disabled=true;}
}
