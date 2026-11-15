// Product layer: agent-led workspace, evidence-based quality, and compute visibility.
const activeStatuses = ['running', 'queued', 'waiting', 'paused', 'preparing'];
const duration = v => v == null ? '—' : v < 60 ? `${Math.round(v)} 秒` : `${Math.floor(v / 60)} 分 ${Math.round(v % 60)} 秒`;
function needsAttention(r) {
  return ['failed', 'interrupted', 'waiting'].includes(r.status) ||
    (r.status === 'done' && (r.quality?.status !== 'passed'));
}
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(S.toastTimer); S.toastTimer = setTimeout(() => $('#toast').hidden = true, 6500);
}
window.addEventListener('unhandledrejection', e => { e.preventDefault(); toast(e.reason?.message || '操作未完成，请稍后重试。'); });
function updateNav() {
  $('#workspaceNav').classList.toggle('selected', ['workspace','run'].includes(S.view));
  $('#agentNav').classList.toggle('selected', S.view === 'agents');
  $('#assemblyNav').classList.toggle('selected', S.view === 'assembly');
  $('#skillsNav')?.classList.toggle('selected', S.view === 'skills');
}
function showWorkspace() {
  $('#assemblyView').hidden=true; $('#skillsView').hidden=true;
  S.view = 'workspace'; S.runId = null; S.detail = null;
  $('#empty').hidden = false; $('#runView').hidden = true; $('#agentsView').hidden = true;
  $('#pageLocation').textContent = '总览'; history.replaceState(null, '', '#workspace');
  updateNav(); renderRunList(); renderWorkspace();
}
function showAgents(id) {
  $('#assemblyView').hidden=true; $('#skillsView').hidden=true;
  S.view = 'agents'; S.runId = null; S.detail = null;
  if (id) S.agentId = id;
  $('#empty').hidden = true; $('#runView').hidden = true; $('#agentsView').hidden = false;
  $('#pageLocation').textContent = '系统 Agent'; history.replaceState(null, '', '#agents');
  updateNav(); renderRunList(); loadAgents();
}
async function loadAgents() {
  S.agents = await api('/api/agents');
  if (!S.agents.some(a => a.id === S.agentId)) S.agentId = S.agents[0]?.id;
  $('#styleAgentSelect').innerHTML = S.agents.map(a => `<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.role)}</option>`).join('');
  if (S.view === 'agents') renderAgents();
  renderWorkspace();
}
function applyReadOnly() {
  const ro = !!S.status?.read_only;
  $('#readOnlyNotice').hidden = !ro;
  if (!ro) return;
  $$('#newRunBtn, [data-mutate], [data-start], [data-stop], #pauseBtn, #resumeBtn, #cancelBtn, #deleteBtn, #retryBtn, #modelSel, #newRunForm button[type=submit], #agentForm button[type=submit], #learnForm button[type=submit], #sourceForm button[type=submit], #consultForm button[type=submit], button[onclick^="approve"], button[onclick^="sendNote"], button[onclick^="openGodot"]').forEach(b => { b.disabled = true; b.title = '只读预览，执行操作已关闭'; });
}
function qualityBadge(q) { return q ? `<span class="quality-badge ${esc(q.status)}">${esc(q.label)}</span>` : '<span class="muted">尚无验收记录</span>'; }
function renderWorkspace() {
  if (S.view !== 'workspace') return;
  const active = S.runs.filter(r => activeStatuses.includes(r.status));
  const attention = S.runs.filter(needsAttention);
  const passed = S.runs.filter(r => r.quality?.status === 'passed');
  const next = [...S.runs].sort((a,b) => (a.status === 'waiting' ? -1 : b.status === 'waiting' ? 1 : 0)).filter(needsAttention).slice(0,4);
  const agent = S.agents[0];
  $('#empty').innerHTML = `<div class="page-eyebrow">CREATIVE OPERATIONS / 工作台</div>
    <div class="page-heading"><div><h1>从想法，到有风格的世界。</h1><p>选择一个 Agent，推进创作，保留每一次判断。</p></div><button class="primary" data-action="create" data-mutate>发起创作 ↗</button></div>
    <div class="workspace-stats"><div><small>执行中的任务</small><strong>${active.length.toString().padStart(2,'0')}</strong><span>包含排队与人工确认</span></div><div class="attention-stat"><small>需要你的判断</small><strong>${attention.length.toString().padStart(2,'0')}</strong><span>中断、审批与验收缺口</span></div><div><small>已记录检查通过</small><strong>${passed.length.toString().padStart(2,'0')}</strong><span>以验收证据为准</span></div></div>
    <div class="home-grid"><section class="card priority-card"><div class="section-title"><div><span class="page-eyebrow">NEXT ACTION</span><h2>接下来做什么</h2></div><span class="small muted">${attention.length} 项待处理</span></div>
      ${next.length ? next.map(r => `<button class="action-row" data-run="${esc(r.id)}"><span class="action-indicator">${r.status === 'waiting' ? '◇' : '↗'}</span><span><b>${esc(r.title)}</b><small>${r.status === 'waiting' ? '检查目标图并确认继续' : r.mode === 'learn' && r.status === 'done' ? '学习草稿已生成，前往系统 Agent 审核' : r.status === 'failed' || r.status === 'interrupted' ? esc(r.error || '查看阻塞原因，从对应阶段继续') : `${r.quality?.failed_count || 0} 项未通过 · ${r.quality?.unknown_count || 0} 项待验证`}</small></span><span class="arrow">→</span></button>`).join('') : '<div class="empty-state">当前没有待处理任务。可以发起一次新的创作。</div>'}
    </section><section class="card agent-teaser"><span class="page-eyebrow">YOUR STYLE PARTNER</span><img src="/static/world.svg" alt="程序化岛屿风格示意"><h2>${esc(agent?.name || '风格 Agent')}</h2><p>${esc(agent?.description || '由风格规则引领设计与构建。')}</p><button data-action="agents">认识你的 Agent ↗</button></section></div>
    <section class="card"><div class="section-title"><h2>最近创作</h2><span class="small muted">执行状态与验收结果分开呈现</span></div><div class="table-wrap"><table class="work-table"><thead><tr><th>任务 / 项目</th><th>工作类型</th><th>执行状态</th><th>验收结果</th><th></th></tr></thead><tbody>${S.runs.slice(0,8).map(r => `<tr><td><button class="text-link" data-run="${esc(r.id)}">${esc(r.title)}</button><small>${esc(r.options?.project || '独立创作')} · ${fmtDate(r.created)}</small></td><td>${esc(MODE_CN[r.mode] || r.mode)}</td><td><span class="badge">${esc(STATUS_CN[r.status] || r.status)}</span></td><td>${qualityBadge(r.quality)}</td><td><button data-run="${esc(r.id)}" aria-label="打开 ${esc(r.title)}">↗</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">还没有创作记录。</td></tr>'}</tbody></table></div></section>`;
  applyReadOnly();
}
function renderResources() {
  const st = S.status; if (!st) return;
  const c = st.compute; const job = c?.active;
  const running = st.services.filter(s => ['ready','starting'].includes(s.state));
  const reserved = running.reduce((n,s) => n + s.mem_gb,0);
  const budget = st.memory.service_budget_gb ?? st.memory.budget_gb;
  const recent = (c?.recent || []).slice(-4).reverse();
  $('#computePanel').innerHTML = `<div class="compute-state"><span class="dot ${job ? 'running' : ''}"></span>${job ? '正在执行' : '等待下一项工作'}<span class="small muted">单任务调度</span></div><div class="compute-job">${esc(job?.operation || '算力空闲')}<small>${job ? `${esc(STAGE_CN[job.stage] || '独立调用')} · ${duration(job.elapsed_seconds)}` : '模型可以驻留，重任务按序执行'}</small></div><div class="resource-numbers"><div><b>${c?.queued?.length || 0}</b><small>算力排队</small></div><div><b>${st.pipeline?.queued?.length || 0}</b><small>任务排队</small></div></div><div class="budget-label">模型驻留估算 <b>${reserved.toFixed(0)} / ${budget ?? '—'} GB</b></div><div class="bar"><i style="width:${Math.min(100,budget ? reserved / budget * 100 : 0)}%"></i></div><p class="small muted">为引擎预留 ${st.memory.engine_reserve_gb ?? '—'} GB；估算不等于实际 GPU 占用。</p>${recent.length ? `<div class="rail-subtitle">最近执行</div>${recent.map(j => `<div class="recent-compute"><span>${esc(j.operation)}</span><small>${j.state === 'done' ? duration(j.execution_seconds) : esc(STATUS_CN[j.state] || j.state)}</small></div>`).join('')}` : ''}`;
}
function renderRunSummary() {
  const d = S.detail; if (!d) return;
  const revision=d.run.options.revision;
  $('#revisionBanner').hidden=!revision;
  $('#revisionBanner').innerHTML=revision ? `<b>独立改版 · 原作保留</b><p>${revision.changes.length} 项已选建议 · 需要重新验收</p><button class="text-link" data-run="${esc(revision.parent_id)}">查看原作 ↗</button> <button class="text-link" data-run="${esc(revision.consultation_id)}">查看顾问提案 ↗</button> <button onclick="setTab('compare')">查看原作与改版对照 →</button><details><summary>本次改版要求</summary><p>必须保留：${esc(revision.preserve || '原作身份')}</p><ol>${revision.changes.map(c=>`<li>${esc(c.title)}：${esc(c.change)}<br>验收：${esc(c.acceptance)}</li>`).join('')}</ol></details>` : '';
  $('#addToGameBtn').hidden = d.run.status!=='done' || d.quality?.status!=='passed' || !['asset','scene','scene3d','oskar'].includes(d.run.mode);
  $('#inviteAgentBtn').hidden = ['learn','consult'].includes(d.run.mode);
  $('#inviteAgentBtn').disabled = activeStatuses.includes(d.run.status);
  const q = d.quality; const completed = d.stages.filter(s => ['done','skipped'].includes(s.status)).length;
  const profile = d.run.options?.style_profile;
  const issue = q?.issues?.[0];
  $('#runSummary').innerHTML = `<div class="run-facts"><div><small>风格引领</small><b>${esc(profile?.name || '历史任务')}</b><span>${profile ? `版本 ${esc(profile.version)}${profile.knowledge ? ' · 知识 ' + esc(profile.knowledge.version) : ''}` : '未保存 Agent 版本'}</span></div><div><small>流程进度</small><b>${completed} <em>/ ${d.stages.length}</em></b><span>${esc(STAGE_CN[d.run.current_stage] || (completed === d.stages.length ? '阶段执行已结束' : '等待阶段开始'))}</span></div><div><small>质量状态</small>${qualityBadge(q)}<button class="text-link" data-action="quality" title="查看每一项检查">${q?.failed_count || 0} 项未通过 · ${q?.unknown_count || 0} 项待验证 →</button></div></div>${issue ? `<div class="quality-callout"><div><b>${esc(q.label)}</b><ul class="quality-issues">${q.issues.map(c => `<li><span class="quality-badge ${c.passed === false ? 'blocked' : 'unverified'}">${c.passed === false ? '未通过' : '待验证'}</span> <b>${esc(c.name)}</b>${c.detail ? `<small> · ${esc(String(c.detail).slice(0, 160))}</small>` : ''}</li>`).join('')}</ul></div><button data-action="quality">查看检查与修复入口 →</button></div>` : ''}`;
  $$('#tabs [data-tab]').forEach(b => { b.hidden = ['learn','consult'].includes(d.run.mode) && !['overview','log','deliver'].includes(b.dataset.tab); });
  $('#tabs [data-tab=compare]').hidden=!revision;
  if ((S.tab === 'compare' && !revision) || (['learn','consult'].includes(d.run.mode) && !['overview','log','deliver'].includes(S.tab))) S.tab = 'overview';
}
function renderQuality() {
  const q = S.detail?.quality; if (!q) return;
  const active = activeStatuses.includes(S.detail.run.status);
  $('#tab-quality').innerHTML = `<div class="card"><div class="section-title"><div><span class="page-eyebrow">QUALITY GATE</span><h2>让交付有据可查</h2></div>${qualityBadge(q)}</div><p class="muted small">${esc(q.scope)}</p><div class="table-wrap"><table><thead><tr><th>检查项</th><th>结果</th><th>证据 / 原因</th><th>下一步</th></tr></thead><tbody>${q.checks.map(c => `<tr><td>${esc(c.name)}</td><td><span class="quality-badge ${c.passed === true ? 'passed' : c.passed === false ? 'blocked' : 'unverified'}">${c.passed === true ? '通过' : c.passed === false ? '未通过' : '未验证'}</span></td><td class="check-detail">${esc(c.detail || '以当前保存的记录为准')}</td><td>${c.passed !== true ? `<button data-retry-stage="${esc(c.stage)}" ${active ? 'disabled' : ''} data-mutate>${['model','assemble'].includes(c.stage) ? '追加 2 轮' : '从此处重跑'}</button><small>${esc(STAGE_CN[c.stage] || c.stage)}</small>` : '—'}</td></tr>`).join('')}</tbody></table></div></div>`;
  applyReadOnly();
}
function renderComparison() {
  const d=S.detail, comparison=d?.comparison;
  if(!comparison)return;
  const baseline=comparison.baseline, current=comparison.current;
  const checkList=q=>q ? `<div class="comparison-checks">${q.checks.map(c=>`<div><span class="quality-badge ${c.passed === true ? 'passed' : c.passed === false ? 'blocked' : 'unverified'}">${c.passed === true ? '通过' : c.passed === false ? '未通过' : '待验证'}</span><b>${esc(c.name)}</b><small>${esc(c.detail || '以保存记录为准')}</small></div>`).join('')}</div>` : '';
  const pictures=images=>images?.length ? `<div class="comparison-images">${images.map(i=>`<figure><a href="${fileUrl(i.path)}" target="_blank" rel="noopener"><img src="${fileUrl(i.path)}" alt="${esc(i.label)}" loading="lazy"></a><figcaption>${esc(i.label)}</figcaption></figure>`).join('')}</div>` : '<div class="empty-state">尚无渲染样本</div>';
  $('#tab-compare').innerHTML=`<section class="card"><span class="page-eyebrow">REVISION REVIEW</span><h2>原作与改版，各自用证据说话。</h2><p class="muted small">${esc(comparison.notice)}</p><div class="comparison-columns"><section><div class="section-title"><h3>原作 · 邀请时快照</h3>${qualityBadge(baseline?.quality)}</div>${baseline ? `<p class="small muted">固定于 ${fmtDate(baseline.captured)} · 后续原作变化不影响这里</p>${pictures(baseline.images)}${checkList(baseline.quality)}` : '<p class="empty-state">原作快照不可用，请查看历史提案。不会使用当前原作替代。</p>'}</section><section><div class="section-title"><h3>独立改版 · 当前记录</h3>${qualityBadge(current.quality)}</div><p class="small muted">${esc(STATUS_CN[d.run.status])} · 新输出需要独立验收</p>${pictures(current.images)}${checkList(current.quality)}</section></div></section><section class="card"><h3>逐项核对已选建议</h3><p class="muted small">下列是审核要求；模型检查通过不代表这些建议已全部实现。</p>${d.run.options.revision.changes.map(c=>`<article class="consultation-change"><h3>${esc(c.title)}</h3><p>${esc(c.change)}</p><p><b>人工复核：</b>${esc(c.acceptance)}</p></article>`).join('')}</section>`;
}

async function retryStage(stage) {
  const d = S.detail; const options = {};
  if (stage === 'model') {
    options.revisit_assets = [...new Set(d.quality.issues.filter(c => c.asset).map(c => c.asset))];
    options.model_max_rounds = Math.max(0,...Object.values(d.state.models || {}).flatMap(m => (m.rounds || []).map(r => r.round))) + 2;
  }
  if (stage === 'assemble') options.godot_max_rounds = Math.max(0,...(d.state.assemble?.rounds || []).map(r => r.round)) + 2;
  await post(`/api/runs/${d.run.id}/retry`, {from_stage:stage,options});
  toast('已加入队列，将从指定阶段继续。'); refreshSoon();
}
const CONSULT_AREAS = {visual:'视觉与配色',space:'空间与关卡',character:'角色与道具',gameplay:'玩法与反馈',narrative:'叙事与节奏'};
function openConsult() {
  if (!S.detail || activeStatuses.includes(S.detail.run.status)) return;
  S.consultParent=S.detail.run.id;
  $('#consultForm').reset();
  $('#consultTarget').textContent='审阅作品：'+S.detail.run.title;
  $('#consultAgent').innerHTML=S.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.role)}</option>`).join('');
  $('#consultAreas').innerHTML=Object.entries(CONSULT_AREAS).map(([id,label])=>`<label class="check-row"><input type="checkbox" name="area" value="${id}" ${id==='visual'?'checked':''}>${label}</label>`).join('');
  $('#consultError').textContent=''; $('#consultDlg').showModal();
}
function consultationCard() {
  const d=S.detail, evidence=d.run.options.consultation, p=d.state.consultation;
  if(!evidence)return '<div class="card">审阅快照不可用。</div>';
  const executable=!!evidence.revision_support?.available;
  const choiceKey=d.run.id+':'+(p?.version || 'pending');
  const selected=S.proposalChoices?.[choiceKey] || [];
  const children=S.runs.filter(r=>r.options?.revision?.consultation_id===d.run.id && r.options.revision.proposal_version===p?.version);
  const followups=children.length ? `<div class="consultation-followups"><b>已创建 ${children.length} 个独立改版</b>${children.map(r=>`<button class="action-row" data-run="${esc(r.id)}"><span><b>${esc(r.title)}</b><small>${esc(STATUS_CN[r.status])} · ${r.options.revision.changes.length} 项建议</small></span>${qualityBadge(r.quality)}<span>查看 →</span></button>`).join('')}</div>` : '';
  const heading=p?'修改提案，等待你的判断。':d.run.status==='queued'?'顾问审阅已排队。':d.run.status==='running'?'Agent 正在审阅作品。':'审阅尚未完成。';
  return `<section class="card consultation-result"><span class="page-eyebrow">DESIGN CONSULTATION</span><h2>${heading}</h2>${followups}<p>${esc(p?.summary || d.run.brief)}</p><div class="consultation-context"><button class="text-link" data-run="${esc(evidence.parent_id)}">原作：${esc(evidence.title)} ↗</button><span>固定快照 · ${fmtDate(evidence.captured)}</span><span>${evidence.images.length} 张渲染样本</span></div><p class="muted small">审阅范围：${evidence.areas.map(a=>esc(CONSULT_AREAS[a])).join(' · ')}<br>保留要求：${esc(evidence.preserve || '未另外指定')}</p><details class="consultation-evidence"><summary>查看此次审阅的固定证据</summary><p class="muted small">${esc(evidence.quality.label)} · 原作状态指纹 ${esc(evidence.state_digest)}</p><div class="thumbs">${evidence.images.map(i=>`<a href="${fileUrl(i.path)}" target="_blank" rel="noopener"><img src="${fileUrl(i.path)}" alt="${esc(i.label)}"><span>${esc(i.label)}</span></a>`).join('')}</div><p class="muted small">${evidence.images.length?'这些是最近保存的渲染样本，不保证是最终导出版本。':'未附图：本次依据已记录方案与验收事实，不能声称完成视觉审阅。'}</p></details>${p ? `<div class="notice">${executable ? '勾选要执行的视觉或角色建议，创建独立改版。原作保留；新概念图需确认，新输出重新验收。' : esc(evidence.revision_support?.reason || '这份提案未保存模型快照，暂不可直接改版。')}</div>${p.changes.map(c=>`<article class="consultation-change"><div class="module-meta"><span>${esc(CONSULT_AREAS[c.area])}</span><span>${children.some(r=>r.options.revision.changes.some(x=>x.id===c.id)) ? '已加入改版 · 结果需验收' : '建议 · 尚未安排'}</span></div><h3>${executable && ['visual','character'].includes(c.area) ? `<label class="check-row"><input type="checkbox" name="proposedChange" value="${esc(c.id)}" ${selected.includes(c.id) ? 'checked' : ''} data-mutate><span>${esc(c.title)}</span></label>` : esc(c.title)}</h3><dl><dt>依据</dt><dd>${esc(c.reason)}</dd><dt>建议修改</dt><dd>${esc(c.change)}</dd><dt>如何验收</dt><dd>${esc(c.acceptance)}</dd></dl><p class="small muted">来源：${c.sources.map(id=>esc(p.sources[id] || id)).join('；')}</p></article>`).join('')}${executable ? `<button class="primary" data-revise="${esc(d.run.id)}" data-proposal-version="${esc(p.version)}" data-mutate>按所选建议创建改版 ↗</button>` : ''}` : '<div class="card"><h3>审阅实时输出</h3><div class="live" id="live"></div></div>'}</section>`;
}
function learningCard() {
  const learning = S.detail?.state.learning;
  if (S.detail?.run.mode !== 'learn') return '';
  const status = S.detail.run.status;
  const title = learning ? '新的风格经验，等待你的判断。' : status === 'running' ? 'Agent 正在研究与提炼。' : ['queued','preparing'].includes(status) ? '研究任务已排队。' : '研究尚未完成。';
  return `<div class="card learning-result"><span class="page-eyebrow">STYLE KNOWLEDGE</span><h2>${title}</h2><p>${esc(learning?.summary || '从本地资料和选定创作中提炼可复用的风格规则。')}</p>${learning ? `<ol class="principles">${learning.rules.map(r => `<li>${esc(r.rule)}</li>`).join('')}</ol><button data-action="agents">前往系统 Agent 审核 →</button>` : ''}${(S.detail.run.options.learning_sources || []).length ? `<div class="pinned-sources"><h3>本次研究固定的资料</h3>${S.detail.run.options.learning_sources.map(s=>`<details><summary>${esc(s.title)} · ${s.characters} 字</summary><p class="small muted">来源指纹 ${esc(s.sha256)}</p><pre>${esc(s.text)}</pre></details>`).join('')}</div>` : ''}</div>`;
}
function agentPortrait(agent) {
  if (agent.id !== 'oskar') return '<img src="/static/world.svg" alt="共享构建引擎的程序化世界示意">';
  return `<figure class="oskar-portrait"><div class="portrait-label">OSKAR / WORLD STUDIES <span>01—02</span></div><div class="portrait-pair"><img src="/static/oskar/citadel.png" alt="本项目融合前的高山圣城 Blender 渲染"><img src="/static/oskar/sea-stacks.png" alt="本项目融合前的海蚀岩群 Blender 渲染"></div><figcaption><b>山势 · 城镇 · 生长的规则</b><span>本项目引擎样本 / 2026.09.29 · 融合前渲染</span></figcaption></figure>`;
}
function knowledgeIntro(agent) {
  const k = agent.active?.knowledge; if (!k) return '';
  return `<section class="knowledge-intro"><div><span class="page-eyebrow">INHERITED KNOWLEDGE</span><h3>${esc(agent.signature || k.title)}</h3><p>GenAIWorkPipeline 的构建经验 × TigerMessenger 的研究方法</p></div><button data-agent-tab="knowledge">${k.modules.length} 组方法 · ${k.sources.length} 份来源 ↗</button></section>`;
}
function knowledgePanel(agent) {
  const k = agent.active.knowledge;
  const byId = Object.fromEntries(k.sources.map(s => [s.id,s]));
  const sourceLink = id => `<a href="/api/agents/${encodeURIComponent(agent.id)}/knowledge/${encodeURIComponent(id)}" target="_blank" rel="noopener">${esc(byId[id]?.title || id)} ↗</a>`;
  return `<section class="card knowledge-heading"><div><span class="page-eyebrow">METHOD LIBRARY / ${esc(k.updated)}</span><h2>它学会了怎样判断，而不仅是怎样生成。</h2><p>${esc(k.summary)}</p></div><div class="knowledge-stamp"><b>${k.modules.length.toString().padStart(2,'0')}</b><span>已融合知识模块</span></div></section>
    <div class="knowledge-grid">${k.modules.map((m,i) => `<article class="card knowledge-module"><div class="module-meta"><span>${String(i+1).padStart(2,'0')} / ${esc(m.category)}</span><span>已纳入创作提示</span></div><h3>${esc(m.title)}</h3><p>${esc(m.summary)}</p><details><summary>查看方法、能力与来源</summary><p>${esc(m.guidance)}</p><dl><dt>现在可以使用</dt><dd>${esc(m.available)}</dd><dt>仍需构建或验证</dt><dd>${esc(m.gap)}</dd><dt>怎样验收</dt><dd>${esc(m.validation)}</dd></dl><div class="knowledge-sources">${m.source_ids.map(sourceLink).join('')}</div></details></article>`).join('')}</div>
    <section class="card research-ledger"><h2>知识有来处，成长有边界。</h2><p>${esc(k.coverage)}</p><p class="small muted">融合版本 ${esc(k.version)} · 内容指纹 ${esc(k.digest.slice(0,12))}。任务会保留当时的知识快照；新资料不会改写已有创作。</p><details><summary>查看 ${k.sources.length} 份归档来源</summary>${k.sources.map(s=>`<div class="source-row"><span>${esc(s.origin)}</span>${sourceLink(s.id)}<code>${esc(s.sha256.slice(0,12))}</code></div>`).join('')}</details></section>`;
}
function renderAgents() {
  if (S.view !== 'agents') return;
  const agent = S.agents.find(a => a.id === S.agentId);
  const current = agent?.active;
  const works = S.runs.filter(r => !['learn','consult'].includes(r.mode) && (r.options?.style_agent || 'oskar') === agent?.id);
  const tab = S.agentTab === 'knowledge' && !current?.knowledge ? 'profile' : S.agentTab || 'profile';
  $('#agentsView').innerHTML = `<div class="page-eyebrow">SYSTEM AGENTS / 风格系统</div><div class="page-heading"><div><h1>让每一种风格，拥有自己的 Agent。</h1><p>研究、设计、构建、复盘。把创作经验变成可继承的风格。</p></div><button data-action="new-agent" data-mutate>＋ 添加 Agent</button></div><div class="agent-switcher">${S.agents.map(a => `<button class="agent-choice ${a.id === agent?.id ? 'selected' : ''}" data-agent="${esc(a.id)}"><span class="agent-avatar">${esc(a.name.slice(0,1))}</span><span><b>${esc(a.name)}</b><small>${a.kind === 'team' ? '团队 · ' : 'OPC · '}${esc(a.role)}</small></span><i class="dot ready"></i></button>`).join('')}</div>
    ${agent ? `<section class="agent-hero"><div class="agent-hero-copy"><div class="agent-meta"><span class="quality-badge passed">已接入创作流程</span>${window.agentFormBadge?.(agent) || ''}<span>${esc(agent.engine.toUpperCase())} ENGINE</span></div><h2>${esc(agent.name)}<span> / ${esc(agent.role)}</span></h2><p>${esc(agent.description)}</p><div class="agent-tags">${agent.tags.map(t => `<span>${esc(t)}</span>`).join('')}</div><div class="agent-actions"><button class="primary" data-create-agent="${esc(agent.id)}" data-mutate>由它引领创作 ↗</button><button data-learn-agent="${esc(agent.id)}" data-mutate>安排学习与研究</button></div></div>${agentPortrait(agent)}</section><div class="agent-ledger"><div><span>当前风格版本</span><b>${esc(current.version === 'baseline' ? '基础规则 / v0' : current.version)}</b></div><div><span>风格规则</span><b>${current.rules.length} 条</b></div><div><span>学习版本</span><b>${agent.profiles.length} 份</b></div><div><span>关联创作</span><b>${works.length} 次</b></div></div>
    <div class="agent-process"><span>01 <b>参考研究</b></span><i>→</i><span>02 <b>风格档案</b></span><i>→</i><span>03 <b>方案与构建</b></span><i>→</i><span>04 <b>质量验收</b></span><i>↺</i><span>05 <b>经验提炼</b></span></div>
    <nav class="tabs agent-tabs"><button data-agent-tab="profile" class="${tab === 'profile' ? 'active' : ''}">风格档案</button><button data-agent-tab="config" class="${tab === 'config' ? 'active' : ''}">配置</button><button data-agent-tab="team" class="${tab === 'team' ? 'active' : ''}">${agent.kind === 'team' ? '团队与协同 <span>' + (agent.members?.length || 0) + '</span>' : 'OPC 与效能'}</button>${current.knowledge ? `<button data-agent-tab="knowledge" class="${tab === 'knowledge' ? 'active' : ''}">方法与能力 <span>${current.knowledge.modules.length}</span></button>` : ''}<button data-agent-tab="sources" class="${tab === 'sources' ? 'active' : ''}">学习资料 <span>${(agent.sources || []).filter(s=>!s.archived).length}</span></button><button data-agent-tab="learning" class="${tab === 'learning' ? 'active' : ''}">学习与版本 <span>${agent.profiles.length}</span></button><button data-agent-tab="works" class="${tab === 'works' ? 'active' : ''}">创作样本</button></nav>
    ${tab === 'config' ? (window.agentConfigPanel?.(agent) || '') : tab === 'team' ? (window.teamPanel?.(agent) || '') : tab === 'sources' ? sourceLibrary(agent) : tab === 'knowledge' ? knowledgePanel(agent) : tab === 'profile' ? `${knowledgeIntro(agent)}<div class="grid2"><section class="card"><div class="section-title"><h2>它如何创造</h2><span class="page-eyebrow">STYLE RULES</span></div><ol class="principles">${current.rules.map(r => `<li>${esc(r)}</li>`).join('')}</ol></section><section class="card"><h2>学习与应用的边界</h2><div class="knowledge-step"><span>01</span><div><b>资料变成规则</b><p>研究已有参考文档与创作记录，每条学习规则保留来源。</p></div></div><div class="knowledge-step"><span>02</span><div><b>审核后应用</b><p>学习产出先保存为草稿。你选择应用的版本，才会影响新创作。</p></div></div><div class="knowledge-step"><span>03</span><div><b>创作可以复现</b><p>任务保存 Agent 与规则版本快照，后续学习不改变已有任务。</p></div></div><p class="muted small">当前是知识与规则学习，不是模型权重训练。${esc(agent.inspiration)}</p></section></div>${window.directionPanel?.(agent) || ''}` : tab === 'learning' ? `<section class="card"><div class="section-title"><div><h2>让经验成为下一次创作的起点</h2><p class="small muted">选择一个版本应用；也可以随时回到基础规则。</p></div><button data-activate-agent="${esc(agent.id)}" data-version="baseline" data-mutate ${current.version === 'baseline' ? 'disabled' : ''}>恢复基础规则</button></div>${agent.profiles.length ? agent.profiles.map(p => `<article class="profile-version"><div class="section-title"><div><b>${esc(p.summary || '风格学习版本')}</b><small>${fmtDate(p.created)} · ${esc(p.version)} · ${p.rules.length} 条规则</small></div><button data-activate-agent="${esc(agent.id)}" data-version="${esc(p.version)}" data-mutate ${current.version === p.version ? 'disabled' : ''}>${current.version === p.version ? '当前使用' : '审核并应用'}</button></div><ol class="principles">${p.rules.map(r => `<li>${esc(r.rule)}<small>来源：${esc(r.sources.join(' · '))}</small></li>`).join('')}</ol><button class="text-link" data-run="${esc(p.run_id)}">查看学习过程 →</button></article>`).join('') : '<div class="empty-state"><span class="empty-symbol">◇</span><h3>第一份学习档案，从一次研究开始。</h3><p>选定参考资料与历史作品，让 Agent 提炼自己的方法。</p><button data-learn-agent="' + esc(agent.id) + '" data-mutate>安排第一次学习 ↗</button></div>'}</section>` : `<section class="card"><h2>以作品检验风格</h2><p class="muted small">历史任务未记录 Agent 时，归入 Oskar 以便检索；这不代表已有学习版本被应用。</p>${works.slice(0,12).map(r => `<button class="action-row" data-run="${esc(r.id)}"><span class="action-indicator">◇</span><span><b>${esc(r.title)}</b><small>${fmtDate(r.created)} · ${esc(MODE_CN[r.mode])}</small></span>${qualityBadge(r.quality)}<span>↗</span></button>`).join('') || '<div class="empty-state">还没有关联作品。发起一次由它引领的创作。</div>'}</section>`}` : '<div class="empty-state">尚无可用 Agent。</div>'}`;
  applyReadOnly();
}
function libraryNote(agent) {
  const L = agent.library; if (!L) return '';
  const c = L.counts || {};
  return `<div class="card library-note"><h3>内置资料库 · ${c.documents || 0} 份文档</h3><p class="small">X 原帖 ${c.x_posts || 0} 条 · 图片/视频 ${c.media || 0} 个（${Math.round((c.media_bytes || 0) / 1048576)} MB）· 评审页 ${c.pages || 0} 个 · 数据 ${c.data || 0} 份。${esc(L.note || '')}</p><p class="small muted">保存在 <code>${esc(L.path)}</code>，整理于 ${esc(L.built)}，来自 ${esc(L.origin_root)}。内置资料只读，可直接勾选加入研究（单份超过 8000 字时取节选）。</p></div>`;
}
function sourceLibrary(agent) {
  const rows = agent.sources || [];
  return `<section class="card source-library"><div class="section-title"><div><h2>为这个 Agent 建立资料库</h2><p class="muted small">保存作品分析、访谈摘录或你的设计笔记。资料加入研究后会固定快照，归档不影响已有任务。</p></div><button data-import-source="${esc(agent.id)}" data-mutate>＋ 导入资料</button></div>${libraryNote(agent)}${rows.length ? rows.map(s=>`<article class="source-document ${s.archived ? 'archived' : ''}"><div class="source-document-heading"><div><h3>${esc(s.title)}</h3><small>${s.builtin ? `内置资料 · ${esc(s.group)}` : s.archived ? '已归档' : '可加入研究'} · ${s.characters} 字 · ${fmtDate(s.created)}</small></div>${s.builtin ? '' : `<button data-source-id="${esc(s.id)}" data-source-agent="${esc(agent.id)}" data-archive="${!s.archived}" data-mutate>${s.archived ? '恢复' : '归档'}</button>`}</div><div class="source-document-links"><a href="/api/agents/${encodeURIComponent(agent.id)}/sources/${encodeURIComponent(s.id)}" target="_blank" rel="noopener">查看保存正文 ↗</a>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">原始来源 ↗</a>` : s.builtin ? `<span title="${esc(s.origin)}">来源：${esc(s.origin.split('/').slice(-2).join('/'))}</span>` : '<span>来源：用户笔记</span>'}<code title="${esc(s.sha256)}">SHA ${esc(s.sha256.slice(0,12))}</code></div></article>`).join('') : '<div class="empty-state"><h3>从一份值得研究的资料开始。</h3><p>支持粘贴文字或导入 UTF-8 的 .md / .txt 文件；每份最多 8000 字。</p></div>'}<p class="muted small">来源链接仅用于追溯，不会自动抓取网页。导入资料后，点击「安排学习与研究」选定本次使用的资料。</p></section>`;
}
function openSourceImport(agentId) {
  S.sourceAgent = agentId;
  $('#sourceForm').reset(); $('#sourceError').textContent = '';
  $('#sourceDlg').showModal();
}
function openLearn(agentId) {
  S.learningAgent = agentId;
  const sources = (S.agents.find(a=>a.id===agentId)?.sources || []).filter(s=>!s.archived);
  $('#learningSources').innerHTML = sources.map(s=>`<label class="check-row"><input type="checkbox" name="source" value="${esc(s.id)}"><span>${esc(s.title)} <small>· ${s.builtin ? '内置 · ' : ''}${s.characters} 字</small></span></label>`).join('') || '<p class="muted small">尚未导入资料，可先在「学习资料」中添加。内置参考文档仍会参与研究。</p>';
  $('#learningRuns').innerHTML = S.runs.filter(r => !['learn','consult'].includes(r.mode) && !activeStatuses.includes(r.status)).map(r => `<option value="${esc(r.id)}">${esc(r.title)} · ${esc(r.quality?.label || r.status)}</option>`).join('');
  $('#learnDlg').showModal();
}
async function newCreation(agentId) {
  $('#newRunBtn').click();
  if (agentId) { $('#styleAgentSelect').value = agentId; $('#styleAgentSelect').dispatchEvent(new Event('change', {bubbles: true})); }
}
// Delegated controls survive live refreshes. IDs from the server are never embedded as executable code.
document.addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  try {
    if (b.dataset.run) return await selectRun(b.dataset.run);
    if (b.dataset.agent) { S.agentId = b.dataset.agent; return renderAgents(); }
    if (b.dataset.agentTab) { S.agentTab = b.dataset.agentTab; return renderAgents(); }
    if (b.dataset.createAgent) return newCreation(b.dataset.createAgent);
    if (b.dataset.revise) {
      const selected=$$('[name=proposedChange]:checked').map(i=>i.value);
      if(!selected.length)throw new Error('请先选择要执行的修改建议。');
      b.disabled=true;
      const result=await post(`/api/runs/${b.dataset.revise}/revise`,{version:b.dataset.proposalVersion,selected});
      await loadRuns(); await selectRun(result.id); toast('独立改版已创建，原作保留，新结果将重新验收。'); return;
    }
    if (b.dataset.action === 'invite-agent') return openConsult();
    if (b.dataset.importSource) return openSourceImport(b.dataset.importSource);
    if (b.dataset.sourceId) {
      b.disabled=true;
      await post(`/api/agents/${b.dataset.sourceAgent}/sources/${b.dataset.sourceId}/archive`, {archived:b.dataset.archive==='true'});
      await loadAgents(); toast('资料状态已更新，已有研究快照不受影响。'); return;
    }
    if (b.dataset.learnAgent) return openLearn(b.dataset.learnAgent);
    if (b.dataset.retryStage) { b.disabled = true; return await retryStage(b.dataset.retryStage); }
    if (b.dataset.activateAgent) {
      b.disabled = true;
      await post(`/api/agents/${b.dataset.activateAgent}/activate`, {version:b.dataset.version});
      toast('风格版本已应用，将用于新创作。'); return await loadAgents();
    }
    if (b.dataset.action === 'agents') return showAgents();
    if (b.dataset.action === 'create') return newCreation();
    if (b.dataset.action === 'quality') return setTab('quality');
    if (b.dataset.action === 'new-agent') return window.openAgentWizard ? openAgentWizard() : $('#agentDlg').showModal();
  } catch (error) { b.disabled = false; toast(error.message); }
});
$('#workspaceNav').onclick = showWorkspace;
$('#agentNav').onclick = () => showAgents();
$('#manageServices').onclick = openSvc;
$('#resourceAccess').onclick = openSvc;
$('#logoutBtn').onclick = async () => { await post('/api/auth/logout'); location.assign('/'); };
$('#runSearch').oninput = renderRunList;
$('#retrySel').onchange = e => S.retryStage = e.target.value;
$$('#runFilters button').forEach(b => b.onclick = () => { S.runFilter = b.dataset.filter; $$('#runFilters button').forEach(x => x.classList.toggle('on',x===b)); renderRunList(); });
document.addEventListener('change', e => {
  if(e.target.name !== 'proposedChange' || !S.detail?.state.consultation) return;
  const key=S.detail.run.id+':'+S.detail.state.consultation.version;
  S.proposalChoices ||= {};
  S.proposalChoices[key]=$$('[name=proposedChange]:checked').map(i=>i.value);
});
document.addEventListener('input', e => { if (e.target.id === 'noteInput') S.noteDraft = e.target.value; });
$('#agentForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target; const b = $('button[type=submit]',f); b.disabled = true;
  try {
    const agent = await post('/api/agents', {name:f.elements.name.value, description:f.description.value, principles:f.principles.value.split('\n').map(x=>x.trim()).filter(Boolean)});
    S.agentId = agent.id; S.agentTab = 'profile'; f.reset(); $('#agentDlg').close(); await loadAgents(); toast('新的风格 Agent 已创建。');
  } catch (error) { toast(error.message); } finally { b.disabled = false; }
};
$('#learnForm').onsubmit = async e => {
  e.preventDefault(); const f=e.target; const b=$('button[type=submit]',f); b.disabled=true;
  try {
    const runs = [...$('#learningRuns').selectedOptions].map(o=>o.value);
    if (runs.length>5) throw new Error('最多选择 5 个历史运行。');
    const sources = [...f.querySelectorAll('[name=source]:checked')].map(i=>i.value);
    if (sources.length>8) throw new Error('每次最多选择 8 份学习资料。');
    const result = await post(`/api/agents/${S.learningAgent}/learn`, {brief:f.brief.value,runs,sources});
    $('#learnDlg').close(); f.reset(); await loadRuns(); await selectRun(result.id); toast('研究任务已加入执行队列。');
  } catch(error) { toast(error.message); } finally { b.disabled=false; }
};
$('#sourceFile').onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  $('#sourceError').textContent='';
  try {
    if (!/\.(md|txt)$/i.test(file.name) || file.size>65536) throw new Error('请选择不超过 64 KB 的 .md / .txt 文件。');
    const text = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer());
    if (!text.trim() || text.length>8000 || text.includes('\0')) throw new Error('资料需要是有效文字，正文最多 8000 字。');
    $('#sourceText').value=text;
    if (!$('#sourceTitle').value) $('#sourceTitle').value=file.name.replace(/\.(md|txt)$/i,'').slice(0,160);
  } catch(error) { $('#sourceError').textContent=error.message; e.target.value=''; }
};
$('#sourceForm').onsubmit = async e => {
  e.preventDefault(); const f=e.target, b=$('button[type=submit]',f); if(b.disabled)return; b.disabled=true;
  $('#sourceError').textContent='';
  try {
    await post(`/api/agents/${S.sourceAgent}/sources`, {title:f.elements.title.value,text:f.elements.text.value,url:f.elements.url.value});
    $('#sourceDlg').close(); f.reset(); S.agentTab='sources'; await loadAgents(); toast('资料已保存，可以加入下一次研究。');
  } catch(error) { $('#sourceError').textContent=error.message; } finally {b.disabled=false;}
};
$('#consultForm').onsubmit = async e => {
  e.preventDefault(); const f=e.target,b=$('button[type=submit]',f); if(b.disabled)return; b.disabled=true;
  $('#consultError').textContent='';
  try {
    const areas=[...f.querySelectorAll('[name=area]:checked')].map(i=>i.value);
    if(!areas.length)throw new Error('至少选择一个审阅范围。');
    const result=await post(`/api/runs/${S.consultParent}/consult`,{agent_id:f.agent.value,brief:f.brief.value,areas,preserve:f.preserve.value});
    $('#consultDlg').close(); await loadRuns(); await selectRun(result.id); toast('顾问已受邀，原作与风格版本已固定。');
  }catch(error){$('#consultError').textContent=error.message;}finally{b.disabled=false;}
};
async function startWorkspace() {
  const user=await api('/api/auth/status'); $('#userName').textContent=user.user || '';
  await Promise.all([loadStatus(),loadRuns(),loadAgents()]);
  const hash=location.hash.slice(1);
  if(hash==='assembly') await showAssembly(); else if(hash==='skills') await showSkills(); else if(hash==='agents') showAgents(); else if(hash && hash!=='workspace' && S.runs.some(r=>r.id===hash)) await selectRun(hash); else showWorkspace();
  setInterval(loadStatus,5000);
  // 运行中每 15s 拉一次详情，避免仅有 stream 事件时步进条不更新
  setInterval(() => {
    const st = S?.detail?.run?.status;
    if (S?.runId && ['running','waiting','paused','queued'].includes(st)) {
      if (typeof loadDetail === 'function') loadDetail();
    }
  }, 15000);
  connect();
}
startWorkspace();
