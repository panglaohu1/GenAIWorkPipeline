// Oskar 导演的多场景世界：世界圣经、逐站目标图评审、逐场景趋近曲线、世界整合与导演档案。
const ATLAS_ROLE = { opening: '开场', rise: '上升', climax: '高潮', resolve: '收束', interlude: '间奏' };
const ATLAS_STOP = { converged: '已收敛', plateau: '停滞收工', max_rounds: '达到最大轮数', no_edits: '无可执行修改',
  build_failed: '初稿构建失败', director_done: '导演主动收工' };
const ATLAS_ACTION = { edit: '受限修改', replan: '推翻重写', done: '宣布完成' };

function atlasDims(a, key = 'rubric') { return (a?.direction?.[key] || []); }
function atlasScore(v) { return typeof v === 'number' && isFinite(v) ? v.toFixed(1) : '—'; }

function atlasBars(scores, dims) {
  if (!scores) return '';
  return `<div class="atlas-bars">${dims.map(d => { const v = scores[d.id] ?? 0;
    return `<div title="${esc(d.ask || '')}"><span>${esc(d.name)}</span><i><b style="width:${Math.max(0, Math.min(100, v * 10))}%" class="${v >= 7 ? 'ok' : v >= 5 ? 'mid' : 'low'}"></b></i><em>${v.toFixed(0)}</em></div>`; }).join('')}</div>`;
}

// 趋近曲线：导演分（实线）与客观指数（虚线，按本场景范围归一）；空心点 = 被回滚的一轮
function atlasCurve(history, pass) {
  const pts = (history || []).filter(h => typeof h.director === 'number' || typeof h.objective === 'number');
  if (pts.length < 1) return '';
  const W = 260, H = 84, P = 8, n = Math.max(1, pts.length - 1);
  const x = i => P + (W - 2 * P) * (pts.length === 1 ? 0.5 : i / n);
  const y = v => H - P - (H - 2 * P) * Math.max(0, Math.min(1, v / 10));
  const objs = pts.map(h => h.objective).filter(v => typeof v === 'number');
  const lo = Math.min(...objs), hi = Math.max(...objs), span = hi - lo || 1;
  const yo = v => H - P - (H - 2 * P) * (0.15 + 0.7 * (v - lo) / span);
  const dir = pts.map((h, i) => typeof h.director === 'number' ? `${x(i)},${y(h.director)}` : null).filter(Boolean).join(' ');
  const obj = objs.length ? pts.map((h, i) => typeof h.objective === 'number' ? `${x(i)},${yo(h.objective)}` : null).filter(Boolean).join(' ') : '';
  return `<svg class="atlas-curve" viewBox="0 0 ${W} ${H}" role="img" aria-label="趋近曲线：导演分与客观相似度">
    <line x1="${P}" x2="${W - P}" y1="${y(pass)}" y2="${y(pass)}" class="pass"/>
    <text x="${W - P}" y="${y(pass) - 3}" text-anchor="end">通过线 ${pass}</text>
    ${obj ? `<polyline points="${obj}" class="obj"/>` : ''}
    <polyline points="${dir}" class="dir"/>
    ${pts.map((h, i) => typeof h.director === 'number' ? `<circle cx="${x(i)}" cy="${y(h.director)}" r="3.2" class="${h.accepted ? 'acc' : 'rej'}"><title>第 ${h.round} 轮 · 导演分 ${h.director} · ${esc(h.decision || '')}</title></circle>` : '').join('')}
  </svg><div class="atlas-legend"><span class="dir">导演分</span><span class="obj">客观相似度（相对）</span><span>○ 回滚</span></div>`;
}

window.atlasOverviewCard = function () {
  const a = S.detail?.state?.atlas; if (!a) return `<div class="card"><h3>世界构想</h3><div class="muted">${esc(S.detail?.run?.options?.style_profile?.name || 'Oskar')} 正在构想世界…</div></div>`;
  const b = a.bible || {}; const w = a.world || {};
  return `<div class="card atlas-bible"><div class="section-title"><div><span class="page-eyebrow">WORLD BIBLE · ${esc(a.agent || 'Oskar')}${a.inherited_from ? ' · 继承 Oskar 导演档案' : ''}</span><h2>${esc(b.title || '')}</h2></div></div>
    <blockquote>${esc(b.vision || '')}</blockquote>
    <ul class="diffs">${(b.pillars || []).map(p => `<li>${esc(p)}</li>`).join('')}</ul>
    <div class="kv"><b>色彩角色</b><span>${esc(b.palette?.notes || '')}${a.palette ? ` <span class="muted small">（锁定自「${esc((b.scenes || []).find(s => s.id === a.palette.from)?.name || a.palette.from)}」）</span>` : ''}</span>
      <b>共享尺度</b><span>格网 ${b.shared?.cell} m · 层高 ${b.shared?.level_h} m · 海面第 ${b.shared?.sea_level} 层</span>
      ${(a.prior_journal || []).length ? `<b>工作室手记</b><span>开工时带上了过往 ${new Set(a.prior_journal.map(e => e.run_id)).size} 个世界的 ${a.prior_journal.length} 条经验</span>` : ''}</div>
    <div class="atlas-journey">${(b.scenes || []).map((s, i) => { const r = a.scenes?.[s.id] || {}; const t = a.targets?.[s.id];
      return `<button class="atlas-stop ${s.role === 'climax' ? 'climax' : ''}" onclick="focusAsset('${esc(s.id)}')" style="--h:${s.intensity * 18}%">
        <small>${i + 1} · ${ATLAS_ROLE[s.role] || s.role}</small><b>${esc(s.name)}</b><i></i>
        <span>${r.status === 'done' ? `导演分 ${atlasScore(r.director)}` : r.status === 'iterating' ? `趋近中 · 第 ${(r.history || []).length} 轮` : t ? `目标图 ${atlasScore(t.director)}` : '待开工'}</span></button>`; }).join('<span class="atlas-arrow">→</span>')}</div>
    ${w.review ? `<div class="small" style="margin-top:10px"><b>世界整合</b>：导演分 ${atlasScore(w.director)}（通过线 ${w.world_pass}）— ${esc(w.review.verdict || '')}</div>` : ''}
  </div>`;
};

function atlasSceneCard(a, s, i) {
  const r = a.scenes?.[s.id] || {}; const dims = atlasDims(a); const conv = a.direction?.convergence || {};
  const c = S.detail.state.concepts?.[s.id]; const target = c ? c.candidates?.[c.selected ?? 0] : null;
  const attempts = c?.oskar?.attempts || [];
  const glb = (S.detail.artifacts || []).filter(x => x.kind === 'glb' && x.meta?.asset === s.id).pop();
  const hist = (r.history || []).slice().reverse();
  return `<div class="card"><h3>${i + 1}. ${esc(s.name)} <span class="small muted">${ATLAS_ROLE[s.role] || s.role} · 张力 ${s.intensity}/5 · ${esc(s.dominant || '')}</span>
      <span class="sp small muted">${r.status === 'done' ? `${ATLAS_STOP[r.stop_reason] || r.stop_reason} · 最佳第 ${r.best_round} 轮` : r.status === 'iterating' ? '趋近中' : '待开工'}</span></h3>
    <div class="grid2"><div>
      <div class="small muted">目标图（Oskar 画、Oskar 选）${attempts.length > 1 ? ` · 重画 ${attempts.length - 1} 次` : ''}</div>
      ${target ? `<div class="thumbs">${thumb(fileUrl(target.path), `导演分 ${atlasScore(target.oskar?.director)}`)}</div>` : '<div class="muted small">尚未出图</div>'}
      ${attempts.map((t, k) => `<div class="small muted">第 ${k + 1} 稿：选 #${t.best} · ${atlasScore(t.director)} 分${t.action === 'redraw' ? ' · 要求重画' : ''} — ${esc(t.direction || '')}</div>`).join('')}
      ${target?.oskar ? atlasBars(target.oskar.scores, dims) : ''}
    </div><div>
      <div class="small muted">趋近：导演分 ${atlasScore(r.start_director ?? r.history?.[0]?.director)} → <b>${atlasScore(r.director ?? Math.max(...(r.history || []).filter(h => h.accepted && typeof h.director === 'number').map(h => h.director), -Infinity) ?? null)}</b>${r.metrics?.dino != null ? ` · DINO ${r.metrics.dino.toFixed(3)}` : ''}${r.forecast?.eta_rounds ? ` · 按当前速度还需约 ${r.forecast.eta_rounds} 轮` : ''}</div>
      ${atlasCurve(r.history, conv.scene_pass ?? 7)}
      ${r.scores ? atlasBars(r.scores, dims) : ''}
      ${r.verdict ? `<div class="atlas-verdict">「${esc(r.verdict)}」</div>` : ''}
      ${glb ? `<model-viewer src="${artUrl(glb.id)}" camera-controls auto-rotate shadow-intensity="1" exposure="1.1"></model-viewer>` : ''}
    </div></div>
    ${r.lessons ? `<div class="small muted">手记：${Object.entries(r.lessons.ops || {}).map(([k, v]) => `${esc(k)} ${v.accepted}/${v.tried}`).join('，') || '—'}</div>` : ''}
    <details ${r.status === 'iterating' ? 'open' : ''}><summary class="small">逐轮记录（${hist.length} 轮）</summary>
    ${hist.map(h => { const fails = (h.checks || []).filter(x => x.required && !x.passed);
      return `<div class="round"><div><div class="small muted">第 ${h.round} 轮</div><div class="score ${h.accepted ? 'pass' : 'fail'}">${atlasScore(h.director)}</div>
        <div class="small ${h.accepted ? '' : 'muted'}">${h.round === 0 ? '初稿' : h.accepted ? '接受' : '回滚'}</div>${r.best_round === h.round ? '<div class="small" style="color:var(--ok)">最佳</div>' : ''}</div>
        <div><div class="small"><b>【${esc(h.phase?.name || '')}】</b>${h.verdict ? ` ${esc(h.verdict)}` : ''}</div>
          <div class="thumbs">${(h.images || []).map(p => thumb(fileUrl(p), p.includes('hero_left') ? '左侧机位' : '正面机位', 'sm')).join('')}</div>
          ${h.metrics ? `<div class="small">DINO ${h.metrics.dino.toFixed(3)} / SigLIP ${h.metrics.siglip.toFixed(3)}</div>` : ''}
          <div class="small" style="color:${fails.length ? 'var(--warn)' : 'var(--ok)'}">硬检查：${fails.length ? esc(fails.map(x => `${x.name} ${x.detail || ''}`).join('；')) : '全部通过'}</div>
          ${h.edits?.length ? `<ul class="diffs">${h.edits.map(d => `<li>${esc(d)}</li>`).join('')}</ul>` : ''}
          ${h.edit_log?.length ? `<div class="small muted">动作：${esc(h.edit_log.join('，'))}</div>` : ''}
          <div class="small muted">判定：${esc(h.decision || '')}</div>
          ${h.error ? `<div class="small" style="color:var(--warn)">${esc(h.error)}</div>` : ''}</div></div>`; }).join('')}
    </details></div>`;
}

function atlasWorldCard(a) {
  const w = a.world; if (!w) return '';
  const dims = atlasDims(a, 'world_rubric');
  const glb = (S.detail.artifacts || []).filter(x => x.kind === 'glb' && x.meta?.asset === 'oskar_atlas').pop();
  const g = a.godot;
  const pl = w.form === 'planet' ? w.planet : null;
  return `<div class="card"><h3>世界整合${pl ? ' · 小行星' : ''}<span class="sp small muted">${w.review ? `导演分 ${atlasScore(w.director)} · 通过线 ${w.world_pass}` : '整合中'}</span></h3>
    ${pl ? `<div class="small muted">海平面半径 ${pl.radius.toFixed(0)} 米 · ${(pl.terrain_links || []).length} 段地势连接${(pl.terrain_links || []).length ? `（${pl.terrain_links.map(t => ({terraces: "阶梯台地", ridge: "山脊垭口", isthmus: "低地地峡"}[t.kind] || t.kind)).join("、")}）` : ""} · ${pl.scenes.map(s => `${esc(s.id)} 纬 ${s.lat.toFixed(0)}° 经 ${s.lon.toFixed(0)}°`).join('，')}</div>` : ''}
    ${(w.faces || []).length ? `<div class="thumbs" style="margin:8px 0">${w.faces.map(p => thumb(fileUrl(p), '星球上的一站', 'sm')).join('')}</div>` : ''}
    <div class="grid2"><div>${(w.images || []).length ? `<div class="thumbs">${w.images.map(p => thumb(fileUrl(p), '整合全景', 'wide')).join('')}</div>` : ''}
      ${w.review ? atlasBars(w.review.scores, dims) + `<div class="atlas-verdict">「${esc(w.review.verdict || '')}」</div>` : ''}
      ${(w.checks || []).map(c => `<div class="small" style="color:${c.passed ? 'var(--ok)' : c.required ? 'var(--warn)' : 'var(--muted)'}">${esc(c.name)}：${esc(c.detail)}${c.required ? '' : '（建议）'}</div>`).join('')}
      ${w.consistency ? `<div class="small">客观风格一致性（两两 SigLIP）：均值 ${w.consistency.mean}，最低 ${w.consistency.min.siglip}</div>` : ''}
      ${Object.entries(w.review?.scene_notes || {}).map(([k, v]) => `<div class="small muted">下次 · ${esc(k)}：${esc(v)}</div>`).join('')}
    </div><div>${glb ? `<model-viewer src="${artUrl(glb.id)}" camera-controls auto-rotate shadow-intensity="1" exposure="1.1"></model-viewer>` : ''}
      ${(w.journey || []).length ? `<div class="small muted">旅程：${w.journey.map(j => esc(j.name)).join(' → ')}</div>` : ''}
      ${g ? `<div class="small" style="color:${g.ok ? 'var(--ok)' : 'var(--warn)'}">Godot：${g.ok ? '核对通过' : '未通过'}${g.fps ? ` · ${Math.round(g.fps)} FPS` : ''}</div><div class="thumbs">${(g.shots || []).map((p, i) => thumb(fileUrl(p), (g.labels || [])[i] || '', 'sm')).join('')}</div>` : ''}
    </div></div>
    ${(w.history || []).length > 1 ? `<div class="small muted">排布迭代：${w.history.map(h => `第 ${h.round} 轮 ${atlasScore(h.review?.director)}${h.accepted ? '（采用）' : ''}`).join(' · ')}</div>` : ''}
  </div>`;
}

window.atlasModelView = function () {
  const a = S.detail.state.atlas; const scenes = a?.bible?.scenes || [];
  const focus = S.modelFocus;
  const pills = `<div class="pills"><button class="pill ${!focus ? 'on' : ''}" onclick="focusAsset(null)">全部</button>${scenes.map(s => `<button class="pill ${focus === s.id ? 'on' : ''}" onclick="focusAsset('${esc(s.id)}')">${esc(s.name)}</button>`).join('')}<button class="pill ${focus === '_compose' ? 'on' : ''}" onclick="focusAsset('_compose')">世界整合</button></div>`;
  const shown = scenes.map((s, i) => [s, i]).filter(([s]) => !focus || focus === s.id);
  return pills + (focus === '_compose' ? '' : shown.map(([s, i]) => atlasSceneCard(a, s, i)).join('')) + (!focus || focus === '_compose' ? atlasWorldCard(a) : '');
};

// 系统 Agent 页：导演档案（评审维度、阶段与趋近策略）
window.directionPanel = function (agent) {
  const d = agent?.direction; if (!d?.rubric) return '';
  const c = d.convergence || {};
  return `<section class="card"><div class="section-title"><div><span class="page-eyebrow">DIRECTION${d.inherited_from ? ' · 继承自 Oskar' : ''}</span><h2>它怎样评判，怎样推进</h2></div></div>
    ${(d.motto || []).length ? `<blockquote class="atlas-motto">${d.motto.map(esc).join('<br>')}</blockquote>` : ''}
    <div class="atlas-rubric">${d.rubric.map(r => `<div><b>${esc(r.name)}</b><small>权重 ${(r.weight * 100).toFixed(0)}%</small><p>${esc(r.ask)}</p></div>`).join('')}</div>
    <div class="agent-process">${(d.phases || []).map((p, i) => `<span>${String(i + 1).padStart(2, '0')}<b>${esc(p.name)}</b></span>`).join('<i>→</i>')}</div>
    <p class="small muted">目标图过线 ${c.target_pass} 分、场景通过 ${c.scene_pass} 分；每场景最多 ${c.max_rounds} 轮，连续 ${c.patience} 轮无提升先推倒重来一次再收工。接受顺序：硬检查 → 客观相似度（+${c.obj_gain}）→ 导演分（持平时 +${c.director_gain} 才算）。</p>
    ${(d.integration?.principles || []).length ? `<ol class="principles">${d.integration.principles.map(p => `<li>${esc(p)}</li>`).join('')}</ol>` : ''}
  </section>`;
};
