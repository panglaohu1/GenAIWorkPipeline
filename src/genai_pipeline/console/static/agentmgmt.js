// Agent 配置与技能库（从 AgentsGroup2026 引入，按本系统改造）：
//  · 系统 Agent 页「配置」：自主级别 / 预算 / 生图后端、人格、模型路由（按子任务）、技能、工具；团队逐个成员配置。
//  · 侧边栏「技能库」：技能卡（生命周期、使用与成功率、版本、来源）、新建 / 编辑 / 发布 / 降级 / 恢复 / 删除、路由试算。
//  · 「添加 Agent」改为四步向导：基础 → 人格 → 技能 → 工具与自主。
(function () {
  const ROLE_CN = { plan: '方案规划', decompose: '场景拆分', blueprint: '蓝图创建', patch: '蓝图补丁', observe: '视觉观察',
    verify: '成对比较', decide: '复核决策', godot: 'Godot 编码' };
  const STAGE_CN2 = { plan: '方案规划', concept: '目标概念图', model: 'Blender 建模', assemble: 'Godot 组装', review: '试玩验收',
    oskar: 'Oskar 场景构建', oskar_plan: 'Oskar 读图', oskar_build: 'Oskar 构建', atlas_brief: '世界构想', atlas_targets: '导演出目标图', atlas_scenes: '逐场景趋近',
    team_plaza: '团队议事', team_retro: '团队复盘', team_research: '团队调研', oskar_godot: 'Godot 场景接入',
    atlas_compose: '世界整合', atlas_godot: 'Godot 世界接入', planet_compose: '星球整合', planet_godot: '星球游戏接入', deliver: '交付打包' };
  const LC_CLASS = { draft: 'wait', published: 'info', verified: 'passed', solidified: 'passed', degraded: 'blocked' };
  const M = { cfg: null, meta: null, skills: null, lifecycle: {}, filter: 'all', q: '', edit: null, wiz: null };
  const pct = x => Math.round((x || 0) * 100) + '%';

  async function loadSkills() {
    const d = await api('/api/skills');
    M.skills = d.skills; M.lifecycle = d.lifecycle; M.tools = d.tools; M.stages = d.stages;
    return d;
  }
  const lcBadge = s => `<span class="quality-badge ${LC_CLASS[s.lifecycle] || ''}">${esc(M.lifecycle[s.lifecycle] || s.lifecycle)}</span>`;
  const stageChips = st => (st || []).map(x => `<span>${esc(STAGE_CN2[x] || x)}</span>`).join('');

  // ───────────── Agent 配置（系统 Agent 页的「配置」标签） ─────────────
  window.agentConfigPanel = function (agent) {
    if (!M.cfg || M.cfg.agent !== agent.id) {
      Promise.all([api(`/api/agents/${encodeURIComponent(agent.id)}/config`), loadSkills()]).then(([d]) => {
        M.cfg = { agent: agent.id, data: d.config }; M.meta = d; renderAgents();
      }).catch(e => toast(e.message));
      return '<div class="card" role="status">正在读取配置…</div>';
    }
    const c = M.cfg.data, meta = M.meta, team = agent.kind === 'team';
    const skillRow = (ids, name) => `<div class="skill-pick">${M.skills.map(s => `<label class="skill-chip ${s.lifecycle === 'degraded' ? 'off' : ''}" title="${esc(s.instructions)}"><input type="checkbox" name="${name}" value="${esc(s.id)}" ${ids.includes(s.id) ? 'checked' : ''}><span>${esc(s.icon)} ${esc(s.name)}</span>${lcBadge(s)}</label>`).join('')}</div>`;
    const modelSel = (val, name) => `<select name="${name}"><option value="">默认（系统路由）</option>${Object.entries(meta.models).map(([k, v]) => `<option value="${k}" ${val === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`;
    return `<form id="agentCfgForm" class="cfg-form">
      <section class="card"><div class="section-title"><div><span class="page-eyebrow">AUTONOMY · BUDGET</span><h2>自主级别与预算</h2><p class="small muted">自主级别决定任务默认在哪些地方停下等你；预算超出只告警、不中断。</p></div></div>
        <div class="autonomy-grid">${Object.entries(meta.autonomy).map(([k, v]) => `<label class="autonomy-card ${+k === c.autonomy_level ? 'selected' : ''}"><input type="radio" name="autonomy_level" value="${k}" ${+k === c.autonomy_level ? 'checked' : ''}><b>L${k}</b><span>${esc(v.replace(/^L\d\s*/, ''))}</span></label>`).join('')}</div>
        <div class="cfg-row"><label>单任务 token 预算（0 = 不限）<input type="number" name="token_budget" min="0" step="10000" value="${c.token_budget || 0}"></label>
        <label>生图后端<select name="image_backend"><option value="" ${!c.image_backend ? 'selected' : ''}>跟随任务设置</option><option value="flux" ${c.image_backend === 'flux' ? 'selected' : ''}>Flux（快）</option></select></label></div></section>
      <section class="card"><div class="section-title"><div><span class="page-eyebrow">PERSONALITY</span><h2>人格</h2><p class="small muted">写进每个阶段的提示词：语气、表达方式、创造性倾向与专长。</p></div></div>
        <div class="cfg-row"><label>语气<input name="tone" maxlength="200" value="${esc(c.personality.tone)}"></label><label>表达方式<input name="response_style" maxlength="200" value="${esc(c.personality.response_style)}"></label>
        <label>创造性 <output id="creatOut">${Number(c.personality.creativity).toFixed(1)}</output><input type="range" name="creativity" min="0" max="1" step="0.1" value="${c.personality.creativity}" oninput="document.getElementById('creatOut').textContent=Number(this.value).toFixed(1)"></label></div>
        <label>专长（逗号分隔，最多 12 个）<input name="expertise" value="${esc((c.personality.expertise_areas || []).join('，'))}"></label></section>
      <section class="card"><div class="section-title"><div><span class="page-eyebrow">MODEL ROUTING</span><h2>模型路由</h2><p class="small muted">按子任务选本机模型；不选就用系统路由。${team ? '成员单独设置的模型优先于这里。' : ''}</p></div></div>
        <div class="cfg-grid">${Object.entries(ROLE_CN).map(([r, n]) => `<label>${esc(n)}${modelSel((c.model || {})[r] || '', 'model_' + r)}</label>`).join('')}</div></section>
      <section class="card"><div class="section-title"><div><span class="page-eyebrow">SKILLS</span><h2>${team ? '全队共用技能' : '技能'}</h2><p class="small muted">每个阶段按任务内容从这里检索最相关的几条，写进提示词（降级的技能不参与）。去「技能库」新建或审核。</p></div><button type="button" onclick="showSkills()">打开技能库 ↗</button></div>
        ${skillRow(c.skills || [], 'skills')}</section>
      <section class="card"><div class="section-title"><div><span class="page-eyebrow">TOOLS</span><h2>工具</h2><p class="small muted">本机真实服务；技能依赖的工具被关掉时，该技能不会被调用。</p></div></div>
        <div class="tool-grid">${Object.entries(meta.tools).map(([k, t]) => `<label class="tool-card"><input type="checkbox" name="tools" value="${k}" ${(c.tools || []).includes(k) ? 'checked' : ''}><b>${esc(t.name)}</b><span>${esc(t.desc)}</span><small>${stageChips(t.stages)}</small></label>`).join('')}</div></section>
      ${team ? `<section class="card"><div class="section-title"><div><span class="page-eyebrow">MEMBERS</span><h2>成员配置</h2><p class="small muted">停用的成员不参加议事和复盘，阶段负责人会回落到组长；组长不能停用。</p></div></div>
        <div class="member-cfg">${(agent.members || []).map(m => { const mc = c.members[m.id] || {}; const lead = m.id === agent.lead; return `<article class="member-cfg-row" data-member="${esc(m.id)}">
          <div class="member-head"><span class="agent-avatar">${esc(m.name.slice(0, 1))}</span><div><b>${esc(m.name)}</b>${lead ? '<em>组长</em>' : ''}<small>${esc(m.title || '')}</small></div>
          <label class="switch"><input type="checkbox" name="m_enabled" ${mc.enabled !== false ? 'checked' : ''} ${lead ? 'disabled' : ''}> 启用</label></div>
          <div class="cfg-row"><label>模型${modelSel(mc.model || '', 'm_model')}</label><label class="grow">补充要求<input name="m_extra" maxlength="1000" value="${esc(mc.extra_instructions || '')}" placeholder="例如：这次只做湖沼，不碰水晶城"></label></div>
          <details><summary>技能（${(mc.skills || []).length}）</summary>${skillRow(mc.skills || [], 'm_skills')}</details></article>`; }).join('')}</div></section>` : ''}
      <div class="cfg-actions"><span class="small muted">保存后对新任务生效；进行中的任务保留创建时的快照。</span><button type="button" onclick="resetAgentCfg()">放弃修改</button><button class="primary" type="submit" data-mutate>保存配置</button></div>
    </form>`;
  };

  function collectCfg(form) {
    const f = new FormData(form), all = n => [...form.querySelectorAll(`[name="${n}"]`)];
    const data = {
      autonomy_level: +f.get('autonomy_level'), token_budget: +f.get('token_budget') || 0, image_backend: f.get('image_backend') || '',
      personality: { tone: f.get('tone'), response_style: f.get('response_style'), creativity: +f.get('creativity'),
        expertise_areas: String(f.get('expertise') || '').split(/[，,]/).map(s => s.trim()).filter(Boolean) },
      model: Object.fromEntries(Object.keys(ROLE_CN).map(r => [r, f.get('model_' + r) || ''])),
      skills: all('skills').filter(i => i.checked && !i.closest('.member-cfg-row')).map(i => i.value),
      tools: all('tools').filter(i => i.checked).map(i => i.value),
    };
    const rows = [...form.querySelectorAll('.member-cfg-row')];
    if (rows.length) data.members = Object.fromEntries(rows.map(r => [r.dataset.member, {
      enabled: r.querySelector('[name=m_enabled]').checked, model: r.querySelector('[name=m_model]').value,
      extra_instructions: r.querySelector('[name=m_extra]').value,
      skills: [...r.querySelectorAll('[name=m_skills]')].filter(i => i.checked).map(i => i.value) }]));
    return data;
  }
  document.addEventListener('submit', async e => {
    if (e.target.id !== 'agentCfgForm') return;
    e.preventDefault();
    const btn = e.target.querySelector('[type=submit]'); btn.disabled = true;
    try {
      M.cfg.data = await post(`/api/agents/${encodeURIComponent(M.cfg.agent)}/config`, collectCfg(e.target));
      toast('配置已保存，新任务生效'); await loadAgents();
    } catch (err) { toast(err.message); } finally { btn.disabled = false; }
  });
  document.addEventListener('change', e => {
    if (e.target.name === 'autonomy_level') document.querySelectorAll('.autonomy-card').forEach(c => c.classList.toggle('selected', c.contains(e.target)));
  });
  window.resetAgentCfg = () => { M.cfg = null; renderAgents(); };

  // ───────────── 技能库（侧边栏第 04 项） ─────────────
  window.showSkills = async function () {
    S.view = 'skills'; S.runId = null; S.detail = null;
    ['#empty', '#agentsView', '#runView', '#assemblyView'].forEach(s => { const el = $(s); if (el) el.hidden = true; });
    $('#skillsView').hidden = false;
    $('#pageLocation').textContent = '技能库'; history.replaceState(null, '', '#skills');
    updateNav(); renderRunList();
    $('#skillsView').innerHTML = '<div class="card" role="status">正在读取技能库…</div>';
    await Promise.all([loadSkills(), S.agents?.length ? null : loadAgents()]);
    renderSkills();
  };

  function renderSkills() {
    if (S.view !== 'skills') return;
    const list = M.skills.filter(s => (M.filter === 'all' || s.lifecycle === M.filter) &&
      (!M.q || (s.name + s.description + s.instructions).toLowerCase().includes(M.q.toLowerCase())));
    const count = k => M.skills.filter(s => s.lifecycle === k).length;
    $('#skillsView').innerHTML = `<div class="page-eyebrow">SKILL LIBRARY / 技能库</div>
      <div class="page-heading"><div><h1>把做对过的事，变成可以复用的技能。</h1><p>每条技能写清做法、适用阶段和依赖工具；任务按阶段与内容检索调用，结算时回写成败，生命周期只按证据升降。</p></div><button class="primary" onclick="editSkill()" data-mutate>＋ 新建技能</button></div>
      <div class="skill-life"><span><b>草稿</b>复盘萃取或手写，不参与调用</span><i>→</i><span><b>已发布</b>参与路由</span><i>→</i><span><b>已验证</b>≥3 次使用，成功率 ≥70%</span><i>→</i><span><b>已固化</b>≥8 次，≥80%</span><i>·</i><span class="down"><b>已降级</b>≥3 次且成功率 <40%，停止调用</span></div>
      <div class="skill-toolbar"><div class="filters">${['all', 'draft', 'published', 'verified', 'solidified', 'degraded'].map(k => `<button class="${M.filter === k ? 'on' : ''}" data-skill-filter="${k}">${k === 'all' ? '全部 ' + M.skills.length : esc(M.lifecycle[k]) + ' ' + count(k)}</button>`).join('')}</div>
        <input class="search" id="skillSearch" type="search" placeholder="搜索技能名称或做法…" value="${esc(M.q)}"></div>
      <div class="skill-grid">${list.map(s => `<article class="card skill-card ${s.lifecycle}">
        <div class="skill-head"><span class="skill-icon">${esc(s.icon)}</span><div><b>${esc(s.name)}</b><small>${esc(s.description || '')}</small></div>${lcBadge(s)}</div>
        <p class="skill-body">${esc(s.instructions)}</p>
        <div class="member-stages">${stageChips(s.stages)}${(s.required_tools || []).map(t => `<span class="tool">${esc(M.tools[t]?.name || t)}</span>`).join('')}</div>
        <div class="skill-stats"><span>使用 <b>${s.usage}</b></span><span>成功率 <b>${s.usage ? pct(s.effectiveness) : '—'}</b></span><span>v${s.version}</span><span>${s.source === 'builtin' ? '内置' : esc(String(s.origin || '用户').replace(/^retro:/, '复盘萃取 · '))}</span></div>
        ${s.adopted_by?.length ? `<small class="muted">用过：${esc(s.adopted_by.join('、'))}</small>` : ''}
        <div class="skill-actions">
          ${s.lifecycle === 'draft' ? `<button data-skill-act="publish" data-skill="${esc(s.id)}" data-mutate>审核发布</button>` : ''}
          ${s.lifecycle === 'degraded' ? `<button data-skill-act="restore" data-skill="${esc(s.id)}" data-mutate>恢复</button>` : `<button data-skill-act="deprecate" data-skill="${esc(s.id)}" data-mutate>降级停用</button>`}
          <button onclick="editSkill('${esc(s.id)}')" data-mutate>${s.source === 'builtin' ? '复制一份改' : '编辑'}</button>
          ${s.source !== 'builtin' ? `<button class="danger" data-skill-act="delete" data-skill="${esc(s.id)}" data-mutate>删除</button>` : ''}
        </div></article>`).join('') || '<div class="empty-state">没有符合条件的技能。</div>'}</div>
      <section class="card route-trial"><div class="section-title"><div><span class="page-eyebrow">ROUTER TRIAL</span><h2>路由试算</h2><p class="small muted">看某个 Agent 在某个阶段、面对某个任务时会调用哪几条技能（BM25 近似 + 阶段匹配 + 生命周期加权）。</p></div></div>
        <form id="routeForm" class="cfg-row"><label>Agent<select name="agent">${(S.agents || []).map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}</select></label>
        <label>阶段<select name="stage">${M.stages.map(x => `<option value="${x}">${esc(STAGE_CN2[x] || x)}</option>`).join('')}</select></label>
        <label>成员（团队）<select name="member"><option value="">全队共用</option>${(S.agents || []).filter(a => a.kind === 'team').flatMap(a => (a.members || []).map(m => `<option value="${esc(m.id)}">${esc(a.name)} · ${esc(m.name)}</option>`)).join('')}</select></label>
        <label class="grow">任务内容<input name="q" placeholder="例如：月夜湖沼，三层岸台和世界树"></label><button class="primary" type="submit">试算</button></form>
        <div id="routeOut"></div></section>`;
    applyReadOnly();
  }

  document.addEventListener('input', e => { if (e.target.id === 'skillSearch') { M.q = e.target.value; clearTimeout(M.t); M.t = setTimeout(() => { renderSkills(); const s = $('#skillSearch'); s.focus(); s.setSelectionRange(s.value.length, s.value.length); }, 250); } });
  document.addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.skillFilter) { M.filter = b.dataset.skillFilter; return renderSkills(); }
    if (b.dataset.skillAct) {
      if (b.dataset.skillAct === 'delete' && !confirm('删除这条技能？已绑定它的 Agent 会自动解绑。')) return;
      b.disabled = true;
      try { await post(`/api/skills/${encodeURIComponent(b.dataset.skill)}/${b.dataset.skillAct}`, {}); await loadSkills(); renderSkills(); toast('技能已更新'); }
      catch (err) { toast(err.message); b.disabled = false; }
    }
  });
  document.addEventListener('submit', async e => {
    if (e.target.id !== 'routeForm') return;
    e.preventDefault();
    const f = new FormData(e.target);
    const d = await api(`/api/skills/route?agent=${encodeURIComponent(f.get('agent'))}&stage=${encodeURIComponent(f.get('stage'))}&member=${encodeURIComponent(f.get('member'))}&q=${encodeURIComponent(f.get('q'))}`);
    $('#routeOut').innerHTML = d.picked.length ? `<ol class="route-picked">${d.picked.map(s => `<li><span>${esc(s.icon)} <b>${esc(s.name)}</b></span>${lcBadge(s)}<code>${s.score}</code></li>`).join('')}</ol>`
      : '<p class="muted small">这个 Agent 在该阶段没有可调用的技能（没绑定、阶段不匹配、依赖工具被关，或已降级）。</p>';
  });

  // 技能编辑（新建 / 编辑 / 复制内置）
  window.editSkill = function (id) {
    const s = id ? M.skills.find(x => x.id === id) : null;
    const copy = s && s.source === 'builtin';
    const d = $('#skillDlg'), f = $('#skillForm');
    f.reset(); $('#skillError').textContent = '';
    f.dataset.id = s && !copy ? s.id : '';
    $('#skillDlgTitle').textContent = !s ? '新建技能' : copy ? `复制「${s.name}」再修改` : `编辑「${s.name}」`;
    f.name.value = s ? (copy ? s.name + '（改）' : s.name) : ''; f.icon.value = s?.icon || '⚡';
    f.description.value = s?.description || ''; f.instructions.value = s?.instructions || '';
    $('#skillStages').innerHTML = M.stages.map(x => `<label class="check-row"><input type="checkbox" name="stages" value="${x}" ${s?.stages?.includes(x) ? 'checked' : ''}><span>${esc(STAGE_CN2[x] || x)}</span></label>`).join('');
    $('#skillTools').innerHTML = Object.entries(M.tools).map(([k, t]) => `<label class="check-row"><input type="checkbox" name="required_tools" value="${k}" ${s?.required_tools?.includes(k) ? 'checked' : ''}><span>${esc(t.name)}</span></label>`).join('');
    d.showModal();
  };
  document.addEventListener('submit', async e => {
    if (e.target.id !== 'skillForm') return;
    e.preventDefault();
    const f = e.target, fd = new FormData(f);
    try {
      await post('/api/skills', { id: f.dataset.id || undefined, name: fd.get('name'), icon: fd.get('icon'), description: fd.get('description'),
        instructions: fd.get('instructions'), stages: fd.getAll('stages'), required_tools: fd.getAll('required_tools') });
      $('#skillDlg').close(); await loadSkills(); renderSkills(); toast(f.dataset.id ? '已保存（正文修改后回到草稿，需重新审核发布）' : '技能已创建（草稿，审核发布后参与调用）');
    } catch (err) { $('#skillError').textContent = err.message; }
  });

  // ───────────── 添加 Agent：四步向导（AgentsGroup2026 五步向导按本系统改造） ─────────────
  const STEPS = ['基础', '人格', '技能', '工具与自主'];
  window.openAgentWizard = async function () {
    await loadSkills();
    const meta = await api('/api/agents/oskar/config');
    M.wiz = { step: 0, meta, d: { name: '', description: '', principles: '', tone: '专业、直接', response_style: '简洁', creativity: 0.5,
      expertise: '', skills: [], tools: Object.keys(meta.tools), autonomy_level: 4 } };
    renderWizard(); $('#agentWizDlg').showModal();
  };
  function renderWizard() {
    const w = M.wiz, d = w.d;
    const body = [
      `<label>Agent 名称<input name="name" required maxlength="60" value="${esc(d.name)}" placeholder="例如：暮色群岛"></label>
       <label>风格描述<textarea name="description" maxlength="1000" placeholder="审美、擅长的世界与创作倾向">${esc(d.description)}</textarea></label>
       <label>初始风格规则 · 每行一条，1–12 条<textarea name="principles" rows="5" placeholder="先确立岛屿剪影，再放置建筑。">${esc(d.principles)}</textarea></label>
       <p class="small muted">形态：OPC（一人公司），复用 Oskar 构建引擎；团队形态目前内置 Greg team。</p>`,
      `<div class="cfg-row"><label>语气<input name="tone" value="${esc(d.tone)}"></label><label>表达方式<input name="response_style" value="${esc(d.response_style)}"></label></div>
       <label>创造性 <output id="wzCreat">${Number(d.creativity).toFixed(1)}</output><input type="range" name="creativity" min="0" max="1" step="0.1" value="${d.creativity}" oninput="document.getElementById('wzCreat').textContent=Number(this.value).toFixed(1)"></label>
       <label>专长（逗号分隔）<input name="expertise" value="${esc(d.expertise)}"></label>`,
      `<p class="small muted">选这个 Agent 具备的技能（只列已发布及以上的；草稿要先在技能库审核）。</p><div class="skill-pick">${M.skills.filter(s => !['draft', 'degraded'].includes(s.lifecycle)).map(s => `<label class="skill-chip"><input type="checkbox" name="skills" value="${esc(s.id)}" ${d.skills.includes(s.id) ? 'checked' : ''}><span>${esc(s.icon)} ${esc(s.name)}</span>${lcBadge(s)}</label>`).join('')}</div>`,
      `<div class="tool-grid">${Object.entries(w.meta.tools).map(([k, t]) => `<label class="tool-card"><input type="checkbox" name="tools" value="${k}" ${d.tools.includes(k) ? 'checked' : ''}><b>${esc(t.name)}</b><span>${esc(t.desc)}</span></label>`).join('')}</div>
       <div class="autonomy-grid">${Object.entries(w.meta.autonomy).map(([k, v]) => `<label class="autonomy-card ${+k === d.autonomy_level ? 'selected' : ''}"><input type="radio" name="autonomy_level" value="${k}" ${+k === d.autonomy_level ? 'checked' : ''}><b>L${k}</b><span>${esc(v.replace(/^L\d\s*/, ''))}</span></label>`).join('')}</div>`][w.step];
    $('#agentWizBody').innerHTML = `<ol class="wiz-steps">${STEPS.map((s, i) => `<li class="${i === w.step ? 'active' : i < w.step ? 'done' : ''}">${i + 1} ${s}</li>`).join('')}</ol>${body}`;
    $('#wizBack').disabled = w.step === 0;
    $('#wizNext').textContent = w.step === STEPS.length - 1 ? '创建 Agent ↗' : '下一步 →';
    $('#agentWizError').textContent = '';
  }
  function saveStep() {
    const f = $('#agentWizForm'), fd = new FormData(f), d = M.wiz.d;
    for (const k of ['name', 'description', 'principles', 'tone', 'response_style', 'expertise']) if (fd.has(k)) d[k] = fd.get(k);
    if (fd.has('creativity')) d.creativity = +fd.get('creativity');
    if (M.wiz.step === 2) d.skills = fd.getAll('skills');
    if (M.wiz.step === 3) { d.tools = fd.getAll('tools'); d.autonomy_level = +fd.get('autonomy_level'); }
  }
  window.wizMove = async function (delta) {
    saveStep();
    const w = M.wiz, d = w.d;
    if (delta > 0 && w.step === 0) {
      const lines = d.principles.split('\n').map(s => s.trim()).filter(Boolean);
      if (!d.name.trim() || !lines.length || lines.length > 12) { $('#agentWizError').textContent = '请填写名称和 1–12 条风格规则'; return; }
    }
    if (delta > 0 && w.step === STEPS.length - 1) {
      try {
        const agent = await post('/api/agents', { name: d.name, description: d.description, principles: d.principles.split('\n').map(s => s.trim()).filter(Boolean) });
        await post(`/api/agents/${encodeURIComponent(agent.id)}/config`, { personality: { tone: d.tone, response_style: d.response_style, creativity: d.creativity,
          expertise_areas: d.expertise.split(/[，,]/).map(s => s.trim()).filter(Boolean) }, skills: d.skills, tools: d.tools, autonomy_level: d.autonomy_level });
        $('#agentWizDlg').close(); S.agentId = agent.id; S.agentTab = 'config'; await loadAgents(); toast('新的 Agent 已创建，可在「配置」继续调整');
      } catch (err) { $('#agentWizError').textContent = err.message; }
      return;
    }
    w.step = Math.max(0, Math.min(STEPS.length - 1, w.step + delta)); renderWizard();
  };
  document.addEventListener('submit', e => { if (e.target.id === 'agentWizForm') { e.preventDefault(); wizMove(1); } });
})();
