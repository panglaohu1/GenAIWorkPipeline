// 智能体的两种形态（OPC 一人公司 / 多智能体团队）与团队管理视图：名册、议事、计划、token 账本、复盘与棘轮。
(function () {
  const STAGE = { plan: '方案规划', concept: '目标概念图', model: 'Blender 建模', assemble: 'Godot 组装', review: '试玩验收',
    team_plaza: '团队议事', team_retro: '团队复盘' };
  const kTok = n => n >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(n || 0);

  window.agentFormBadge = function (agent) {
    if (agent.kind === 'team') return `<span class="form-badge team">多智能体团队 · ${agent.members?.length || 0} 人</span>`;
    return `<span class="form-badge opc">OPC · 一人公司</span>`;
  };

  function ratchetBlock(agent) {
    const m = agent.ratchet?.metrics?.efficiency;
    return `<section class="card team-ratchet"><div class="section-title"><div><h2>效能棘轮</h2><p class="small muted">效能 = 质量 × 成功率 ÷ 千 token。只有超过历史最好才推进一代；${agent.kind === 'team' ? '推进时成员复盘规则会存成一份学习版本，等你在「学习与版本」里审核应用。' : '每次任务完成后自动结算。'}</p></div>
      <div class="ratchet-stamp"><b>${m ? '第 ' + m.generation + ' 代' : '尚无记录'}</b><span>${m ? '最好效能 ' + Number(m.value).toFixed(4) : '完成一次创作后出现'}</span></div></div>
      ${m?.history?.length ? `<ol class="ratchet-history">${m.history.slice(-6).reverse().map(h => `<li><b>G${h.generation}</b> ${Number(h.value).toFixed(4)}<small>${esc(h.evidence?.run || '')} · 质量 ${h.evidence?.quality ?? '—'} · ${kTok(h.evidence?.tokens)} token</small></li>`).join('')}</ol>` : ''}</section>`;
  }

  window.teamPanel = function (agent) {
    if (agent.kind !== 'team') {
      return `<section class="card opc-card"><span class="page-eyebrow">ONE-PERSON COMPANY</span><h2>一个 Agent，做完整件事。</h2><p>${esc(agent.opc || '同一个 Agent 兼任导演、建模与评审。')}</p>
        <div class="opc-roles"><span><b>导演</b>读图 · 出目标图 · 评审</span><span><b>建模</b>场景程序 · 引擎迭代</span><span><b>验收</b>硬检查 · 同机位比对</span></div></section>${ratchetBlock(agent)}`;
    }
    const lead = agent.lead;
    const mg = agent.management || {};
    return `<section class="card team-intro"><span class="page-eyebrow">MULTI-AGENT TEAM / ${esc(mg.source || '')}</span><h2>先议事，再分工；先确认，再开工。</h2>
      <div class="team-flow"><span>01 <b>议事</b>ORID：事实与风险</span><i>→</i><span>02 <b>成稿</b>组长归纳计划</span><i>→</i><span>03 <b>五指投票</b>有人反对就修订</span><i>→</i><span>04 <b>你确认</b>计划生效</span><i>→</i><span>05 <b>分工建造</b>负责人 + 评审</span><i>→</i><span>06 <b>复盘</b>效能与棘轮</span></div></section>
      <div class="team-roster">${(agent.members || []).map(m => `<article class="card member-card ${m.id === lead ? 'lead' : ''}">
        <div class="member-head"><span class="agent-avatar">${esc(m.name.slice(0, 1))}</span><div><b>${esc(m.name)}</b>${m.id === lead ? '<em>组长</em>' : ''}<small>${esc(m.title || '')}</small></div></div>
        <p>${esc(m.focus || '')}</p><p class="small muted">研究：${esc(m.studies || '')}</p>
        <div class="member-stages">${(m.lead_stages || []).map(s => `<span class="own">负责 · ${esc(STAGE[s] || s)}</span>`).join('')}${(m.review_stages || []).map(s => `<span>评审 · ${esc(STAGE[s] || s)}</span>`).join('')}</div></article>`).join('')}</div>
      ${ratchetBlock(agent)}<p class="small muted">${esc(agent.inspiration || '')}</p>`;
  };

  function tokensTable(tok, members) {
    if (!tok || !tok.total) return '';
    const name = id => (members || []).find(m => m.id === id)?.name || id;
    const rows = (obj, label) => Object.entries(obj || {}).sort((a, b) => b[1].total - a[1].total)
      .map(([k, v]) => `<tr><td>${esc(label(k))}</td><td>${kTok(v.total)}</td><td>${v.calls}</td><td><i class="bar" style="width:${Math.round(100 * v.total / tok.total)}%"></i></td></tr>`).join('');
    return `<div class="card"><h3>token 账本 <span class="small muted">共 ${kTok(tok.total)} · ${tok.calls} 次调用</span></h3><div class="grid2">
      <table class="token-table"><tr><th>成员</th><th>token</th><th>次</th><th></th></tr>${rows(tok.by_member, name)}</table>
      <table class="token-table"><tr><th>阶段</th><th>token</th><th>次</th><th></th></tr>${rows(tok.by_stage, s => STAGE[s] || STAGE_CN?.[s] || s)}</table></div></div>`;
  }

  const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? '刚刚' : s < 3600 ? Math.floor(s / 60) + ' 分前' : s < 86400 ? Math.floor(s / 3600) + ' 小时前' : Math.floor(s / 86400) + ' 天前'; };
  const dur = r => r.finished ? Math.max(1, Math.round(r.finished - r.started)) + ' 秒' : '进行中';

  const VCLS = { '已设计': 'plain', '已实现': 'info', '自动检查通过': 'ok', '实景已看': 'ok', '未验收': 'warn' };
  function reviewCard(team, run) {
    const rv = team.review; if (!rv) return '';
    const res = (S.detail?.artifacts || []).find(a => a.kind === 'results');
    return `<div class="card team-review"><h3>组长总评 ${res ? `<a class="small" href="${artUrl(res.id)}" target="_blank" rel="noopener">RESULTS.md ↗</a>` : ''}</h3><p>${esc(rv.summary)}</p>
      ${rv.unfinished?.length ? `<b>尚未完成及下一轮安排</b><ol class="next-round">${rv.unfinished.map(u => `<li><span class="vstat ${u.priority === 'P0' ? 'warn' : 'info'}">${esc(u.priority)}</span> <b>${esc(u.item)}</b> <small>${esc(u.hours)} 小时${u.exit ? ' · 退出条件：' + esc(u.exit) : ''}</small></li>`).join('')}</ol><p class="small muted">估算不是已耗时；未完成项不算进成果。</p>` : ''}</div>`;
  }

  // 子智能体：成员为每项具体工作开出的一次执行（议事发言、负责某阶段、投票、复盘…）
  function subagentPanel(subs, members, prof) {
    if (!subs?.length) return '';
    const name = id => members.find(m => m.id === id)?.name || (id === prof.agent_id ? prof.name : id);
    const open = subs.filter(x => x.status === 'running'), done = subs.filter(x => x.status !== 'running').reverse();
    const row = x => `<li class="sub ${x.status}"><span class="agent-avatar sm">${esc(name(x.member).slice(0, 1))}</span><span><b>${esc(name(x.member))}</b> · ${esc(x.task)}<small>${esc(STAGE[x.stage] || STAGE_CN?.[x.stage] || x.stage || '')} · ${dur(x)} · ${kTok(x.tokens)} token${x.status === 'failed' ? ' · 失败' : ''}</small></span><time>${ago(x.finished || x.started)}</time></li>`;
    return `<div class="card subagents"><h3>子智能体</h3><p class="small muted">成员是常设角色；每接一项具体工作就开一个子智能体执行，结束后留下用时与 token。</p>
      <div class="sub-group"><b>已开启 · ${open.length}</b>${open.length ? `<ul>${open.map(row).join('')}</ul>` : '<p class="muted small">没有已开启的子智能体</p>'}</div>
      <div class="sub-group"><b>完成 · ${done.length}</b><ul>${done.slice(0, 12).map(row).join('')}</ul>${done.length > 12 ? `<details><summary>再显示 ${done.length - 12} 个</summary><ul>${done.slice(12).map(row).join('')}</ul></details>` : ''}</div></div>`;
  }

  // 每个人做了什么：方法来源 / 本次学习并落实 / 交付及状态
  function memberLedger(prof, team, state) {
    const plan = team.plan || prof.team_plan || {};
    const subs = state.subagents || [];
    return `<div class="card"><h3>每个人做了什么</h3><table class="plan-table member-ledger"><tr><th>成员 / 方法来源</th><th>本次学习并落实</th><th>交付及状态</th></tr>${(prof.members || []).map(m => {
      const sp = (team.speeches || []).find(x => x.member === m.id) || {};
      const own = (plan.phases || []).filter(p => p.owner === m.id);
      const rv = (plan.phases || []).filter(p => (p.reviewers || []).includes(m.id));
      const re = (team.retro || []).find(x => x.member === m.id);
      const mine = subs.filter(x => x.member === m.id), tok = mine.reduce((a, x) => a + (x.tokens || 0), 0);
      const vote = (team.votes || []).find(v => v.member === m.id);
      return `<tr><td><b>${esc(m.name)}</b>${m.id === prof.lead ? ' <em class="lead-tag">组长</em>' : ''}<small>${esc(m.studies || '')}</small></td>
        <td>${(re?.learned?.length ? re.learned : (sp.proposals || []).slice(0, 3)).map(x => `· ${esc(x)}`).join('<br>') || '<span class="muted">—</span>'}${re?.learned?.length ? '' : '<small>（议事提案；复盘后换成实际落实）</small>'}</td>
        <td>${(re?.deliverables || []).map(d => `<div class="deliv"><span class="vstat ${VCLS[d.status] || 'warn'}">${esc(d.status)}</span> ${esc(d.item)}${d.evidence ? `<small>证据：${esc(d.evidence)}</small>` : ''}</div>`).join('')}${own.map(p => `负责「${esc(STAGE[p.stage] || p.stage)}」：${esc(p.deliverable || (p.tasks || []).join('；'))}`).join('<br>')}${rv.length ? `<br><small>评审：${rv.map(p => esc(STAGE[p.stage] || p.stage)).join('、')}</small>` : ''}
          <small>${vote ? '投票 ✋' + vote.fingers + ' · ' : ''}${mine.length} 个子智能体 · ${kTok(tok)} token${re ? ' · 复盘 ' + Number(re.score).toFixed(1) + ' 分' : ''}</small>
          ${re?.issues?.length ? `<small class="warn">问题：${esc(re.issues.slice(0, 2).join('；'))}</small>` : ''}</td></tr>`; }).join('')}</table></div>`;
  }

  window.teamRunCard = function () {
    const { run, state } = S.detail || {};
    if (!run) return '';
    const prof = run.options?.style_profile || {};
    const team = state.team || {};
    const set = state.settlement;
    const members = prof.members || team.members || [];
    const name = id => members.find(m => m.id === id)?.name || id;
    let html = '';
    if (prof.kind === 'team') {
      const plan = team.plan || prof.team_plan;
      const waiting = run.status === 'waiting' && run.current_stage === 'team_plaza';
      html += `<div class="card team-run"><h3>${esc(prof.name)} · 议事与计划 ${team.consensus ? `<span class="small muted">五指平均 ${team.consensus.mean} · ${team.consensus.reached ? '达成共识' : '未完全一致'}${team.revisions ? ' · 修订 ' + team.revisions + ' 次' : ''}</span>` : ''}</h3>
        ${waiting ? `<div class="banner"><b>团队计划待你确认</b><span>看过下面的分工与验收后确认，团队才开工。可以附一句修改要求，会写进计划。</span><textarea id="teamNote" rows="2" placeholder="（可选）给团队的修改要求"></textarea><button class="primary" onclick="approveTeamPlan()" data-mutate>确认计划，开工</button></div>` : ''}
        ${team.speeches?.length ? `<details ${plan ? '' : 'open'}><summary>第一轮发言（${team.speeches.length} 人）</summary>${team.speeches.map(s => `<div class="speech"><b>${esc(s.name)}</b><ul>${(s.proposals || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>${s.risks?.length ? `<small>风险：${esc(s.risks.join('；'))}</small>` : ''}</div>`).join('')}</details>` : '<p class="muted small">议事尚未开始。</p>'}
        ${plan ? `<div class="plan-goal"><b>${esc(plan.goal || '')}</b>${plan.scale ? `<span>尺度：${esc(plan.scale)}</span>` : ''}${plan.landmarks ? `<span>地标与抵达：${esc(plan.landmarks)}</span>` : ''}</div>
          <table class="plan-table"><tr><th>阶段</th><th>负责人</th><th>评审</th><th>任务</th><th>验收</th></tr>${(plan.phases || []).map(p => `<tr><td>${esc(STAGE[p.stage] || p.stage)}</td><td><b>${esc(name(p.owner))}</b></td><td>${esc((p.reviewers || []).map(name).join('、'))}</td><td>${esc((p.tasks || []).join('；'))}</td><td>${esc((p.acceptance || []).join('；'))}</td></tr>`).join('')}</table>
          ${plan.risks?.length ? `<div class="small"><b>风险与裁决</b><ul class="diffs">${plan.risks.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}` : ''}
        ${team.votes?.length ? `<div class="votes">${team.votes.map(v => `<span class="vote f${v.fingers}" title="${esc(v.reason)}${v.change ? '｜' + esc(v.change) : ''}">${esc(v.name)} <b>✋${v.fingers}</b></span>`).join('')}</div>` : ''}
        ${team.human_note ? `<p class="small"><b>你的附言：</b>${esc(team.human_note)}</p>` : ''}</div>`;
      if (team.retro?.length) html += `<div class="card"><h3>成员复盘</h3>${team.retro.map(r => `<div class="speech"><b>${esc(r.name)}</b> <span class="score">${Number(r.score).toFixed(1)}</span><small>下次带上：${esc(r.next_rule || '—')}</small>${r.issues?.length ? `<ul>${r.issues.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>`).join('')}</div>`;
    }
    if (prof.kind === 'team') html += reviewCard(team, run);
    if (prof.kind === 'team' && (team.speeches?.length || team.retro?.length)) html += memberLedger(prof, team, state);
    html += subagentPanel(state.subagents, members, prof);
    html += tokensTable(state.tokens, members);
    if (set) html += `<div class="card settlement"><h3>效能结算 <span class="small muted">${set.kind === 'team' ? '团队' : 'OPC'}</span></h3><div class="kv"><b>质量</b><span>${set.quality}</span><b>成功率</b><span>${set.success}</span><b>token</b><span>${kTok(set.tokens)}</span><b>效能</b><span>${Number(set.efficiency).toFixed(4)}</span><b>棘轮</b><span>${set.ratchet?.advanced ? '推进到第 ' + set.ratchet.generation + ' 代' : '未推进'} · ${esc(set.ratchet?.reason || '')}</span></div></div>`;
    return html;
  };

  // 新建创作时选了团队：团队做的是“完整游戏”（议事 → 分工建造 → 复盘），自动切到该模式并提示
  document.addEventListener('change', e => {
    if (e.target?.id !== 'styleAgentSelect') return;
    const a = (S.agents || []).find(x => x.id === e.target.value);
    if (a?.kind !== 'team') return;
    const game = document.querySelector('input[name=mode][value=game]');
    if (game) game.checked = true;
    toast(`${a.name} 会先议事出计划，你确认后再分工建造完整游戏`);
  });

  // 团队调研（按《Mœbius 游戏创作者研究》的调研过程）：结论、成员与研究对象、分级发现、最小样区、尚未覆盖
  window.teamResearchCard = function () {
    const r = S.detail?.state?.team?.research; if (!r) return '';
    const doc = (S.detail.artifacts || []).find(a => a.kind === 'research');
    const KC = { '作者陈述': 'ok', '画面观察': 'info', '本项目推断': 'plain', '待验证': 'warn' };
    return `<div class="card team-research"><h3>团队调研 ${doc ? `<a class="small" href="${artUrl(doc.id)}" target="_blank" rel="noopener">RESEARCH.md ↗</a>` : ''}</h3>
      <p>${esc(r.conclusion)}</p><p class="small muted">证据分级：A 具名作者 / 团队直接陈述 · B 间接影响链或记者归纳 · C 仅外观相似 · 提案 = 本项目推断。标为作者陈述却缺 A/B 来源的，系统自动降为待验证。</p>
      <table class="plan-table"><tr><th>成员</th><th>人物 / 团队</th><th>作品</th><th>影响证据</th><th>本项目最适用部分</th></tr>${r.members.map(x => `<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.creator)}</td><td>${esc(x.game)}</td><td>${esc(x.evidence)}</td><td>${esc(x.applies)}</td></tr>`).join('')}</table>
      ${r.members.map(x => `<details><summary><b>${esc(x.name)}</b> · ${x.findings.length} 条发现 · 样区：${esc(x.proposal.area || '—')}</summary>
        <ul class="findings">${x.findings.map(f => `<li><span class="vstat ${KC[f.kind] || ''}">${esc(f.kind)}</span><span class="grade">${esc(f.grade)}</span> ${esc(f.claim)}<small>${esc([f.source, f.date, f.coverage && '范围：' + f.coverage, f.note].filter(Boolean).join(' · '))}</small></li>`).join('')}</ul>
        <div class="kv small"><b>现状证据</b><span>${esc(x.proposal.current)}</span><b>参考</b><span>${esc(x.proposal.reference)}</span><b>修改点</b><span>${esc(x.proposal.change)}</span><b>复用</b><span>${esc(x.proposal.reuse)}</span><b>固定边界</b><span>${esc(x.proposal.boundary)}</span><b>验收</b><span>${esc(x.proposal.acceptance)}</span></div></details>`).join('')}
      ${r.not_covered?.length ? `<b>尚未覆盖</b><ul class="diffs">${r.not_covered.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
      ${S.detail.state.learning?.skills_drafted?.length ? `<p class="small">已生成 ${S.detail.state.learning.skills_drafted.length} 条草稿技能，<button class="text-link" onclick="showSkills()">去技能库审核 →</button></p>` : ''}</div>`;
  };

  window.approveTeamPlan = async function () {
    const note = document.getElementById('teamNote')?.value || '';
    await post(`/api/runs/${S.runId}/approve`, { gate: 'team_plaza', selections: { note } });
    toast('计划已确认，团队开工'); refreshSoon();
  };
})();
