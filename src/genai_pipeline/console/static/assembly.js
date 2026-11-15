// Accepted outputs -> ordered levels + shared assets -> one independent game.
const assemblyRoles = {level:'关卡场景', player:'玩家外观', prop:'共享道具'};
const assemblyDraftKey = 'genai.assembly.draft.v1';
let assemblyDraft = {title:'', brief:'', agent_id:'oskar', form:'planet', selections:[]};
let assemblyCatalog = [], assemblyExcluded = 0, assemblyBusy = false, assemblyLoad = 0;
try {
  const saved = JSON.parse(localStorage.getItem(assemblyDraftKey));
  if (saved && typeof saved.title === 'string' && typeof saved.brief === 'string' && Array.isArray(saved.selections)) {
    assemblyDraft = {title:saved.title.slice(0,120),brief:saved.brief.slice(0,4000),agent_id:String(saved.agent_id || 'oskar'),form:saved.form==='levels'?'levels':'planet',
      selections:saved.selections.filter(p=>p && typeof p.id === 'string' && assemblyRoles[p.role]).slice(0,12)
        .map(p=>({id:p.id,role:p.role,goal:String(p.goal || '').slice(0,600)}))};
  }
} catch {}
function saveAssemblyDraft() {
  try { localStorage.setItem(assemblyDraftKey,JSON.stringify(assemblyDraft)); }
  catch { toast('浏览器无法保存草稿；请保持当前页面打开。'); }
}
async function showAssembly(preselect) {
  const ticket=++assemblyLoad;
  S.view='assembly'; S.runId=null; S.detail=null;
  $('#empty').hidden=true; $('#agentsView').hidden=true; $('#skillsView').hidden=true; $('#runView').hidden=true; $('#assemblyView').hidden=false;
  $('#pageLocation').textContent='游戏组装'; history.replaceState(null,'','#assembly'); updateNav(); renderRunList();
  $('#assemblyView').innerHTML='<div class="card" role="status">正在检查成果与验收记录…</div>';
  try {
    const data=await api('/api/assembly/catalog');
    if(ticket!==assemblyLoad || S.view!=='assembly')return;
    assemblyCatalog=data.items; assemblyExcluded=data.excluded;
    if(preselect && !assemblyDraft.selections.some(p=>p.id===preselect)) {
      const item=assemblyCatalog.find(p=>p.id===preselect);
      if(item && assemblyDraft.selections.length<12) {
        assemblyDraft.selections.push({id:item.id,role:item.default_role,goal:''}); saveAssemblyDraft();
      } else toast('这条产线当前不可用于组装，或已达到 12 条上限。');
    }
    renderAssembly();
  } catch(error) {
    if(ticket===assemblyLoad && S.view==='assembly')$('#assemblyView').innerHTML=`<div class="card"><p role="alert">${esc(error.message)}</p><button onclick="showAssembly()">重新检查</button></div>`;
  }
}
function renderAssembly() {
  $('#assemblyView').innerHTML=`<section class="assembly-hero"><span class="page-eyebrow">GAME ASSEMBLY / 从作品到游戏</span><h1>让每一段创作，<br>成为同一个世界的旅程。</h1><p>选择通过验收的产线成果，用关卡、玩家与道具把它们连接起来。</p><p><button class="primary" type="button" onclick="playDemo()">▶ 试玩星球样例（十二门徒 · 高山圣城 · 新城）</button></p><div class="assembly-steps"><span><b>01</b> 选择成果</span><i>→</i><span><b>02</b> 编排旅程</span><i>→</i><span><b>03</b> 组装与试玩</span></div></section>
  <div class="assembly-layout"><section class="assembly-library"><div class="section-title"><div><span class="page-eyebrow">ACCEPTED OUTPUTS</span><h2>可用成果 <small>${assemblyCatalog.length}</small></h2></div><button id="assemblyRefresh">刷新成果</button></div><p class="small muted">执行完成、已有检查通过，并且模型文件可用。另有 ${assemblyExcluded} 条资产／场景产线未满足条件。</p><input id="assemblySearch" type="search" class="search" aria-label="搜索可用成果" placeholder="搜索场景、资产或运行编号…"><div id="assemblyLibrary" class="assembly-cards"></div></section>
  <section class="assembly-editor card"><div class="section-title"><div><span class="page-eyebrow">YOUR GAME</span><h2>编排游戏</h2></div><span id="assemblyCount" class="quality-badge"></span></div><p class="small muted">草稿保存在当前浏览器。创建时固定成果副本与 Agent 版本。</p>
  <form id="assemblyForm"><label class="assembly-field">游戏名称<input name="title" required maxlength="120" placeholder="例如：从湖沼到云上圣城" value="${esc(assemblyDraft.title)}"></label><label class="assembly-field">引领组装的 Agent<select name="agent_id">${S.agents.map(a=>`<option value="${esc(a.id)}" ${a.id===assemblyDraft.agent_id?'selected':''}>${esc(a.name)}</option>`).join('')}</select></label><fieldset class="assembly-field"><legend>组装方式</legend><label><input type="radio" name="form" value="planet" ${assemblyDraft.form!=='levels'?'checked':''}> 星球：选中的场景贴到同一颗小行星上，Oskar 用地势把它们连起来；小老虎探险收集每站的信，可乘环球有轨电车（F 上下车，C 司机视角）。Oskar 场景按程序重建，山体用新的色场配色</label><label><input type="radio" name="form" value="levels" ${assemblyDraft.form==='levels'?'checked':''}> 关卡串联：按顺序切换独立关卡</label></fieldset><label class="assembly-field">游戏目标与串联要求<textarea name="brief" required maxlength="4000" rows="3" placeholder="玩家是谁？从哪里出发？各关卡如何连接，最终要完成什么？">${esc(assemblyDraft.brief)}</textarea></label>
  <div class="assembly-plan-head"><h3>旅程与共享模块</h3><span class="small muted">用 ↑ ↓ 调整顺序</span></div><div id="assemblyRoute"></div><div id="assemblyPlan"></div>
  <div class="assembly-end"><span>终点</span><b>通关画面 · 重新开始</b></div><p id="assemblyValidation" class="small muted" role="status"></p><p id="assemblyError" class="error" role="alert"></p><button id="assemblyCreate" type="submit" class="primary" data-mutate>创建游戏组装任务 ↗</button><p class="small muted">流程（星球）：成果编排 → 星球整合（重建场景、地势连接、贴球）→ 星球游戏接入 → 人工试玩确认 → 打包；（关卡串联）：成果编排 → Godot 组装 → 自动试玩 → 人工确认全流程 → 打包。${S.status?.read_only?'当前为只读预览：可以编排草稿，执行已关闭。':'提交后进入运行队列，沿用现有算力调度。'}</p></form></section></div>`;
  const form=$('#assemblyForm');
  if(!S.agents.some(a=>a.id===assemblyDraft.agent_id))assemblyDraft.agent_id=S.agents[0]?.id || 'oskar';
  form.addEventListener('input',e=>{
    if(['title','brief','agent_id','form'].includes(e.target.name))assemblyDraft[e.target.name]=e.target.value;
    else if(e.target.dataset.goal){const p=assemblyDraft.selections.find(p=>p.id===e.target.dataset.goal);if(p)p.goal=e.target.value;}
    saveAssemblyDraft();
  });
  form.addEventListener('change',e=>{
    if(e.target.dataset.role){assemblyDraft.selections.find(p=>p.id===e.target.dataset.role).role=e.target.value;saveAssemblyDraft();renderAssemblyPlan();}
  });
  form.onsubmit=createAssembly;
  $('#assemblySearch').oninput=renderAssemblyLibrary;
  $('#assemblyRefresh').onclick=()=>showAssembly();
  renderAssemblyLibrary();renderAssemblyPlan();
}
function renderAssemblyLibrary() {
  const search=($('#assemblySearch').value || '').trim().toLowerCase();
  const items=assemblyCatalog.filter(p=>(p.title+' '+p.id).toLowerCase().includes(search));
  $('#assemblyLibrary').innerHTML=items.map(p=>{
    const chosen=assemblyDraft.selections.some(x=>x.id===p.id);
    return `<article class="assembly-source ${chosen?'chosen':''}"><label class="assembly-pick"><input type="checkbox" data-pick="${esc(p.id)}" ${chosen?'checked':''} aria-label="选择 ${esc(p.title)}"><span>${chosen?'已选入游戏':'加入游戏'}</span></label>${p.preview?`<img class="assembly-preview" src="${artUrl(p.preview)}" loading="lazy" alt="${esc(p.title)} 的历史渲染样本">`:`<div class="assembly-preview assembly-no-image">${p.default_role==='level'?'◈':'◇'}<small>模型成果 · 暂无渲染样本</small></div>`}<div class="assembly-source-info"><span class="quality-badge passed">已有检查通过</span><h3>${esc(p.title)}</h3><p>${esc(MODE_CN[p.mode])} · ${p.model_count} 个模型 · ${(p.bytes/1048576).toFixed(1)} MB</p><button type="button" class="text-link" data-run="${esc(p.id)}">查看原产线与验收 ↗</button></div></article>`;
  }).join('') || `<div class="empty-state">${assemblyCatalog.length?'没有匹配的成果。':'还没有可组装的成果。请先完成资产／场景产线，并通过质量检查。'}</div>`;
}
function assemblyProblem() {
  const parts=assemblyDraft.selections;
  if(!parts.length)return '请从左侧选择成果；至少需要一个关卡场景。';
  if(parts.some(p=>!assemblyCatalog.some(c=>c.id===p.id)))return '部分草稿成果已不可用，请移除后再创建。';
  if(!parts.some(p=>p.role==='level'))return '至少将一个成果设为关卡场景。';
  if(parts.filter(p=>p.role==='player').length>1)return '玩家外观只能选择一个，其余可设为共享道具。';
  if(parts.some(p=>p.role==='player' && assemblyCatalog.find(c=>c.id===p.id).model_count!==1))return '玩家外观需要选择单模型产线。';
  return '';
}
function renderAssemblyPlan() {
  const parts=assemblyDraft.selections;
  const title=p=>assemblyCatalog.find(c=>c.id===p.id)?.title || `已不可用 · ${p.id}`;
  $('#assemblyCount').textContent=`${parts.length} / 12 条产线`;
  const levels=parts.filter(p=>p.role==='level');
  $('#assemblyRoute').innerHTML=levels.length?`<div class="assembly-route"><span>出生</span>${levels.map((p,i)=>`<i>→</i><b title="${esc(title(p))}">${String(i+1).padStart(2,'0')}</b>`).join('')}<i>→</i><span>通关</span></div>`:'';
  let level=0;
  $('#assemblyPlan').innerHTML=parts.map((p,i)=>`<article class="assembly-node"><div class="assembly-node-head"><span class="assembly-number">${p.role==='level'?String(++level).padStart(2,'0'):'＋'}</span><b>${esc(title(p))}</b></div><div class="assembly-node-actions"><select data-role="${esc(p.id)}" aria-label="${esc(title(p))} 的用途">${Object.entries(assemblyRoles).map(([v,label])=>`<option value="${v}" ${p.role===v?'selected':''}>${label}</option>`).join('')}</select><button type="button" data-move="${i}" data-step="-1" aria-label="上移 ${esc(title(p))}" ${i===0?'disabled':''}>↑</button><button type="button" data-move="${i}" data-step="1" aria-label="下移 ${esc(title(p))}" ${i===parts.length-1?'disabled':''}>↓</button><button type="button" data-remove="${esc(p.id)}" aria-label="移除 ${esc(title(p))}">移除</button></div>${p.role==='level'?`<label class="assembly-field small">本关目标／出口条件<input data-goal="${esc(p.id)}" value="${esc(p.goal)}" maxlength="600" placeholder="默认：到达出口，进入下一关"></label>`:'<p class="small muted">所有关卡共享，具体使用方式由游戏目标决定。</p>'}</article>`).join('') || '<div class="assembly-placeholder">◈<b>你的第一关，从这里开始。</b><span>选择场景，再添加角色和道具。</span></div>';
  const problem=assemblyProblem();
  $('#assemblyValidation').textContent=problem || `${levels.length} 个关卡按序串联 · ${parts.length-levels.length} 个共享模块${parts.some(p=>p.role==='player')?'':' · 使用默认可控玩家'}`;
  $('#assemblyCreate').disabled=assemblyBusy || !!problem || !!S.status?.read_only;
  applyReadOnly();
}
$('#assemblyView').addEventListener('change',e=>{
  if(!e.target.dataset.pick)return;
  const id=e.target.dataset.pick, found=assemblyCatalog.find(p=>p.id===id);
  if(e.target.checked){
    if(assemblyDraft.selections.length>=12){e.target.checked=false;toast('最多选择 12 条产线。');return;}
    assemblyDraft.selections.push({id,role:found.default_role,goal:''});
  }else assemblyDraft.selections=assemblyDraft.selections.filter(p=>p.id!==id);
  saveAssemblyDraft();renderAssemblyLibrary();renderAssemblyPlan();
});
$('#assemblyView').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.remove)assemblyDraft.selections=assemblyDraft.selections.filter(p=>p.id!==b.dataset.remove);
  else if(b.dataset.move!==undefined){const i=Number(b.dataset.move),j=i+Number(b.dataset.step);if(j<0 || j>=assemblyDraft.selections.length)return;[assemblyDraft.selections[i],assemblyDraft.selections[j]]=[assemblyDraft.selections[j],assemblyDraft.selections[i]];}
  else return;
  saveAssemblyDraft();renderAssemblyLibrary();renderAssemblyPlan();
});
let assemblyRequest=null;
async function createAssembly(e) {
  e.preventDefault();if(assemblyBusy || S.status?.read_only)return;
  const problem=assemblyProblem();if(problem){$('#assemblyError').textContent=problem;return;}
  assemblyBusy=true;$('#assemblyCreate').disabled=true;$('#assemblyError').textContent='';
  const body=JSON.stringify(assemblyDraft);
  if(assemblyRequest?.body!==body)assemblyRequest={body,id:crypto.randomUUID()};
  try {
    const result=await post('/api/assembly',{...assemblyDraft,request_id:assemblyRequest.id});
    await loadRuns();await selectRun(result.id);toast('组装任务已创建，原产线保留；游戏将独立验收。');
  } catch(error) {if($('#assemblyError'))$('#assemblyError').textContent=error.message;else toast(error.message);}
  finally{assemblyBusy=false;if(S.view==='assembly')renderAssemblyPlan();}
}
function assemblyRunCard() {
  const m=S.detail?.run.options.assembly;if(!m)return '';
  const planetBtn = m.form === 'planet' ? '' : `<p><button class="primary" onclick="planetize()" data-mutate>改用星球方式重建（同一批场景，新建任务）</button></p>`;
  return `<section class="card"><span class="page-eyebrow">COMPOSED GAME / 来源已固定${m.form === 'planet' ? ' · 星球' : ''}</span><h2>${esc(m.title)}</h2>${planetBtn}<p class="small muted">复用 ${m.parts.length} 条已验收产线的模型副本；顺序与用途固定于创建时。整体游戏需重新试玩与验收。</p><ol class="assembly-provenance">${m.parts.map(p=>`<li><b>${esc(assemblyRoles[p.role])}</b> <button class="text-link" data-run="${esc(p.source_id)}">${esc(p.title)} ↗</button><small>${esc(p.goal || (p.role==='level'?'到达出口进入下一关':'跨关卡共享'))} · ${p.assets.length} 个模型</small></li>`).join('')}</ol></section>`;
}
async function planetize() {
  try { const r = await post(`/api/runs/${S.runId}/planetize`, {}); await loadRuns(); await selectRun(r.id); toast('已创建星球组装任务：同一批场景贴到星球上，地势相连，小老虎送信 + 有轨电车'); } catch (e) { toast(e.message); }
}
window.planetize = planetize;
async function approveJourney() {
  await post(`/api/runs/${S.runId}/approve`,{gate:'journey',selections:{journey_played:true}});refreshSoon();
}
$('#assemblyNav').onclick=()=>showAssembly();
$('#addToGameBtn').onclick=()=>showAssembly(S.runId);
