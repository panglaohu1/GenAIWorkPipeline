// GenAI Pipeline 控制台前端（无构建步骤）
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.assign('/'); throw new Error('登录已过期，请重新登录'); }
  if (!r.ok) { const body = await r.text(); let detail = body; try { detail = JSON.parse(body).detail || body; } catch {} throw new Error(detail); }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const fileUrl = (p) => `/api/file?path=${encodeURIComponent(p)}`;
const artUrl = (id) => `/api/artifacts/${id}`;
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "");
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
const STAGE_CN = { planet_compose:"星球整合", planet_godot:"星球游戏接入", assembly_plan:"成果编排", journey:"完整流程试玩确认",  plan: "方案规划", concept: "目标概念图", model: "Blender 建模", assemble: "Godot 组装", review: "试玩验收", deliver: "交付打包", decompose: "拆分子资产", compose: "场景组装", oskar_plan: "读图与场景程序", oskar_build: "引擎构建与迭代", oskar_godot: "Godot 场景接入", world: "Oskar 世界", atlas_brief: "世界构想", atlas_targets: "导演出目标图", atlas_scenes: "逐场景趋近", atlas_compose: "世界整合", atlas_godot: "Godot 世界接入", team_plaza: "团队议事与计划", team_research: "团队调研（证据分级）", team_retro: "团队复盘与效能结算" };
const MODE_CN = { assembly:"游戏组装",  game: "完整游戏", asset: "单个资产", scene: "复杂场景（旧）", scene3d: "场景（图生 3D）", oskar: "场景（Oskar）", atlas: "多场景世界（Oskar 导演）", learn: "风格学习", consult: "顾问审阅" };
const STATUS_CN = { preparing: "准备中", queued: "排队中", running: "运行中", waiting: "等待确认", paused: "已暂停", done: "完成", failed: "失败", cancelled: "已取消", interrupted: "已中断", pending: "待执行", skipped: "跳过", ready: "就绪", starting: "启动中", stopped: "未启动", error: "错误" };

STATUS_CN.done = '流程结束'; STAGE_CN.learn = '研究与风格提炼';
const S = { status: null, runs: [], runId: null, detail: null, events: [], lastEventId: 0, tab: "overview", logFilter: "all", live: [], pendingSel: {}, view: 'workspace', runFilter: 'all', agents: [] };

// ───────── 顶栏：服务状态 ─────────
async function loadStatus() {
  try { S.status = await api("/api/status"); $("#connection").textContent = '服务在线'; } catch { $("#connection").textContent = '连接中断 · 正在重试'; return; }
  const st = S.status;
  const chips = st.services.map((s) => `<span class="chip" data-svc="${s.name}" title="${esc(s.error || s.label)}"><span class="dot ${s.state}"></span>${esc(svcShort(s.name))}<span class="muted">${STATUS_CN[s.state] || s.state}</span></span>`);
  chips.push(`<span class="chip" title="${esc(st.engines.blender.version || "")}"><span class="dot ${st.blender_worker ? "ready" : (st.engines.blender.present ? "" : "error")}"></span>Blender<span class="muted">${st.blender_worker ? "worker 在线" : (st.engines.blender.present ? "按需启动" : "未安装")}</span></span>`);
  chips.push(`<span class="chip" title="${esc(st.engines.godot.version || "")}"><span class="dot ${st.engines.godot.present ? "ready" : "error"}"></span>Godot<span class="muted">${esc((st.engines.godot.version || "").split(".").slice(0, 3).join(".") || "未安装")}</span></span>`);
  const dl = Object.entries(st.downloads || {}).filter(([k, v]) => v && typeof v === "object" && v.state === "downloading");
  for (const [k, v] of dl) chips.push(`<span class="chip" title="${esc(v.repo)}"><span class="dot running"></span>下载 ${esc(k)}<span class="muted">${(v.done / 1e9).toFixed(1)}/${(v.total / 1e9).toFixed(1)} GB · ${v.mbps} MB/s</span></span>`);
  $("#services").innerHTML = chips.join("");
  $$("#services [data-svc]").forEach((el) => (el.onclick = () => openSvc()));
  const m = st.memory;
  $("#mem").innerHTML = `内存 ${m.used_gb}/${m.total_gb} GB<div class="bar"><i style="width:${m.percent}%"></i></div>`;
  if ($("#svcDlg").open && !$("#svcBody details[open]")) renderSvc();
  window.renderResources?.(); window.renderWorkspace?.(); window.applyReadOnly?.();
}
const svcShort = (n) => ({ "llm-main": "LLM（Qwen3.8）", image: "生图" }[n] || n);

function openSvc() { renderSvc(); $("#svcDlg").showModal(); }
function renderSvc() {
  const st = S.status; if (!st) return;
  $("#svcBody").innerHTML = st.services.map((s) => `
    <div class="svc-row">
      <div><div><span class="dot ${s.state}" style="display:inline-block"></span> <b>${esc(s.label)}</b> <span class="muted small">:${s.port} · ${STATUS_CN[s.state] || s.state}${s.rss_gb ? ` · 占用 ${s.rss_gb} GB` : ""}</span></div>
        ${s.missing?.length ? `<div class="small" style="color:var(--warn)">缺少文件：${esc(s.missing.join(", "))}</div>` : ""}
        ${s.error ? `<div class="small" style="color:var(--err)">${esc(s.error)}</div>` : ""}
        <details class="small"><summary>日志</summary><pre class="svc-log" data-log="${s.name}">加载中…</pre></details></div>
      <div class="row">${s.state === "stopped" || s.state === "error" ? `<button data-start="${s.name}">启动</button>` : `<button data-stop="${s.name}" ${s.active_requests ? 'disabled title="当前请求完成后可停止"' : ''}>${s.active_requests ? '使用中' : '停止'}</button>`}</div>
    </div>`).join("");
  $$("[data-start]").forEach((b) => (b.onclick = async () => { b.disabled = true; await post(`/api/services/${b.dataset.start}/start`); loadStatus(); }));
  $$("[data-stop]").forEach((b) => (b.onclick = async () => { b.disabled = true; await post(`/api/services/${b.dataset.stop}/stop`); loadStatus(); }));
  $$("#svcBody details").forEach((d) => d.addEventListener("toggle", async () => {
    if (!d.open) return; const pre = $("pre", d); const r = await api(`/api/services/${pre.dataset.log}/log?lines=120`); pre.textContent = r.text || "(空)"; pre.scrollTop = 1e9;
  }));
  window.applyReadOnly?.();
}

// ───────── 运行列表 ─────────
async function loadRuns() {
  S.runs = await api("/api/runs");
  try { S.projects = await api("/api/projects"); } catch { S.projects = []; }
  renderRunList(); window.renderWorkspace?.();
}
function renderRunList() {
  const byProj = {};
  const query = ($("#runSearch").value || '').trim().toLowerCase();
  const filtered = S.runs.filter(r => `${r.title} ${r.brief} ${r.options?.project || ''}`.toLowerCase().includes(query))
    .filter(r => S.runFilter === 'all' || (S.runFilter === 'active' ? ['running','queued','paused','waiting'].includes(r.status) : window.needsAttention?.(r)));
  for (const r of filtered) (byProj[r.options?.project || ""] ||= []).push(r);
  const order = Object.keys(byProj).sort((a,b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)));
  $("#runCount").textContent = `${filtered.length} / ${S.runs.length}`;
  $("#runList").innerHTML = order.map((pid) => {
    const p = S.projects.find((x) => x.id === pid);
    return `<div class="proj"><div class="proj-h">${esc(p ? p.name : pid || "未归属项目")}<span class="muted small"> · ${byProj[pid].length}</span></div>${byProj[pid].map(runItem).join("")}</div>`;
  }).join("") || `<div class="muted small">没有匹配的创作记录</div>`;
  $$(".run-item").forEach((el) => (el.onclick = () => selectRun(el.dataset.id)));
}
function runItem(r) {
  return `
    <button class="run-item ${r.id === S.runId && S.view === 'run' ? "active" : ""}" data-id="${r.id}">
      <div class="t">${esc(r.title || r.brief)}</div>
      <div class="m"><span class="dot ${r.status}"></span>${STATUS_CN[r.status] || r.status}<span>${MODE_CN[r.mode] || r.mode}</span><span>${fmtDate(r.created)}</span></div>
      <div class="mini-steps">${r.stages.map((s) => `<i class="${s.status}" title="${esc(s.name)}"></i>`).join("")}</div>
      ${r.quality ? `<div class="run-quality ${esc(r.quality.status)}">${esc(r.quality.label)}</div>` : ''}
    </button>`;
}

async function selectRun(id) {
  $('#assemblyView').hidden = true;
  S.view = 'run'; S.retryStage = null; $("#agentsView").hidden = true; $("#skillsView").hidden = true;
  S.detail = null; setTab('overview');
  $("#pageLocation").textContent = '创作任务'; window.updateNav?.();
  S.runId = id; S.events = []; S.lastEventId = 0; S.live = []; S.pendingSel = {}; S.modelFocus = null; S.noteDraft = "";
  history.replaceState(null, "", `#${id}`);
  $("#empty").hidden = true; $("#runView").hidden = false; $("#log").innerHTML = "";
  await Promise.all([loadDetail(), loadEvents()]);
  if (S.runId === id && S.view === 'run') $('main').scrollTop = 0;
  loadRuns();
}

async function loadDetail() {
  if (!S.runId || S.view !== 'run') return;
  const id = S.runId; const detail = await api(`/api/runs/${id}`);
  if (S.runId !== id || S.view !== 'run') return;
  S.detail = detail;
  renderRun();
}

async function loadEvents() {
  const id = S.runId; const evs = await api(`/api/runs/${id}/events?after=${S.lastEventId}&limit=800`);
  if (S.runId !== id || S.view !== 'run') return;
  for (const e of evs) addEvent(e, false);
  renderLog(); renderLive();
}

// ───────── 运行详情 ─────────
function renderRun() {
  const { run, stages, control } = S.detail;
  const labels = S.status?.stage_labels || STAGE_CN;
  $("#runTitle").textContent = run.title || run.brief.slice(0, 40);
  $("#runMeta").innerHTML = `<span class="badge"><span class="dot ${run.status}"></span>${STATUS_CN[run.status] || run.status}</span> · ${MODE_CN[run.mode] || run.mode} · ${esc(run.id)} · 创建于 ${fmtDate(run.created)}`;
  $("#modelSel").value = control.model || run.options.model || "main";
  const active = ["running", "waiting", "paused", "queued"].includes(run.status);
  $("#pauseBtn").hidden = run.status === "paused" || !active; $("#resumeBtn").hidden = run.status !== "paused";
  $("#cancelBtn").disabled = !active;
  $("#deleteBtn").disabled = active || ["queued", "waiting", "paused", "preparing"].includes(run.status);
  $("#retrySel").innerHTML = stages.map((s) => `<option value="${s.name}">从「${labels[s.name] || s.name}」重跑</option>`).join("");
  const firstUndone = stages.find((s) => s.status !== "done");
  if (firstUndone) $("#retrySel").value = firstUndone.name;
  if (S.retryStage) $("#retrySel").value = S.retryStage;
  $("#retryBtn").disabled = active && run.status !== "queued";
  $("#stepper").innerHTML = stages.map((s, i) => {
    const d = s.detail || {};
    const dur = s.finished && s.started ? `${Math.round(s.finished - s.started)}s` : s.started && s.status === "running" ? `已运行 ${Math.round(Date.now() / 1000 - s.started)}s` : "";
    return `<div class="step ${s.status}"><div class="n">${i + 1} · ${STATUS_CN[s.status] || s.status}</div><div class="l"><span class="dot ${s.status}"></span>${labels[s.name] || s.name}</div><div class="d">${esc(d.error ? "错误：" + d.error : dur)}</div></div>`;
  }).join("");
  let banner = "";
  const gate = stages.find((s) => s.status === "waiting")?.detail?.waiting_for;
  if (run.status === "waiting" && gate === "integrate") banner = `<div class="banner"><b>等待确认接入</b><span>将把 GLB 复制到 ${esc(run.options.integrate_to || "")}/assets/pipeline/${esc(run.id)}/（不覆盖原文件）。请先在「建模」页检查结果。</span><button class="primary" onclick="approve('integrate')">确认接入</button><button onclick="setTab('model')">去查看</button></div>`;
  else if (run.status === "waiting" && gate === "journey") banner = `<div class="banner"><b>完整游戏流程待人工试玩</b><span>请逐关检查目标、出口、关卡切换、最终通关与重新开始。完成后确认，才会正式打包。</span><button onclick="openGodot(false)" data-mutate>打开游戏试玩</button><button class="primary" onclick="approveJourney()" data-mutate>已试玩全流程，确认通过</button></div>`;
  else if (run.status === "waiting" && gate === "team_plaza") banner = `<div class="banner"><b>团队计划待你确认</b><span>团队已议事并投票，计划（分工、验收）在「概览」页。确认后才开工。</span><button class="primary" onclick="setTab('overview')">去看计划</button></div>`;
  else if (run.status === "waiting") banner = `<div class="banner"><b>等待人工确认</b><span>请在「概念图」页检查/选择目标图，可重新生成，然后确认继续。</span><button class="primary" onclick="approve()">确认并继续</button><button onclick="setTab('concept')">去查看</button></div>`;
  else if (run.status === "queued") {   // 运行队列一次只跑一个任务：说清楚在等谁
    const busy = (S.runs || []).filter((r) => r.id !== run.id && ["running", "waiting", "paused"].includes(r.status));
    banner = `<div class="banner"><b>排队中</b><span>${busy.length ? `前面有 ${busy.map((r) => `「${esc(r.title)}」（${STATUS_CN[r.status] || r.status}）`).join("、")} 占用运行队列（一次只运行一个任务），它结束或被取消后才轮到本任务。` : "等待运行队列启动。"}</span>${busy.map((r) => `<button onclick="selectRun('${r.id}')">去看「${esc(r.title)}」</button>`).join("")}</div>`;
  }
  else if (run.status === "failed" || run.status === "interrupted") banner = `<div class="banner err"><b>${STATUS_CN[run.status]}</b><span>${esc(run.error || "")}</span><button onclick="retry()">从失败阶段重跑</button></div>`;
  // 有 Godot 工程的任务（游戏、星球组装、多场景世界、Oskar 场景）：直接从这里进入游戏试玩
  const st = S.detail.state || {};
  // 只在游戏已经能运行时给“进入游戏试玩”：运行中的任务工程还在生成；自动试玩有脚本错误时打开只会是灰屏
  const gameReady = st.planet?.godot?.ok || st.atlas?.godot?.ok || st.oskar?.godot?.ok || st.final_review?.test?.hard_ok === true;
  const playable = ["game", "assembly", "atlas", "oskar"].includes(run.mode) && !active && !!gameReady;
  if (playable && !banner && run.options.playtest?.confirmed) banner = `<div class="banner"><b>已试玩确认</b><span>下载与发布已开放</span><button class="primary" onclick="setTab('deliver')">去下载与发布</button><button onclick="openGodot(false)">▶ 再玩一次</button></div>`;
  else if (playable && !banner) banner = `<div class="banner"><b>${st.planet || st.atlas?.world?.form === "planet" ? "星球游戏已就绪" : "游戏 / 场景已接入 Godot"}</b><span>${st.planet || st.atlas?.world?.form === "planet" ? "WASD 移动 · 空格跳 · 拖动鼠标转视角 · 每站找一封信 · 靠近电车按 F 上车，车上按 C 切换司机视角" : "在本机 Godot 中运行"}</span><button class="primary" onclick="openGodot(false)">▶ 进入游戏试玩</button><button onclick="confirmPlaytest()" data-mutate>已试玩，开放下载与发布</button><button onclick="openGodot(true)">在 Godot 编辑器中打开</button></div>`;
  else if (playable && run.status === "failed") banner = banner.replace("</div>", `<button onclick="openGodot(false)">▶ 进入游戏试玩</button></div>`);
  else if (!banner && !active && ["game", "assembly"].includes(run.mode) && st.final_review?.test && st.final_review.test.hard_ok !== true)
    banner = `<div class="banner err"><b>游戏还不能运行</b><span>自动试玩发现脚本 / 运行错误（打开只会是灰屏）：${esc(String(st.final_review.test.facts || "").slice(0, 160))}</span>${run.options.assembly && run.options.assembly.form !== "planet" ? `<button class="primary" onclick="planetize()">改用星球方式重建</button>` : ""}<button onclick="setTab('quality')">查看检查</button></div>`;
  $("#banner").innerHTML = banner;
  window.renderRunSummary?.();
  renderTab();
  window.applyReadOnly?.();
}

function setTab(t) { S.tab = t; $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t)); $$(".tab").forEach((el) => (el.hidden = el.id !== `tab-${t}`)); renderTab(); }
function renderTab() {
  if (!S.detail) return;
  ({ overview: renderOverview, concept: renderConcept, model: renderModel, game: renderGame, quality: window.renderQuality, compare: window.renderComparison, log: renderLog, deliver: renderDeliver, scenecheck: window.renderSceneCheck }[S.tab] || (() => {}))();
  window.applyReadOnly?.();
}

function renderOverview() {
  const { run, state } = S.detail; const spec = state.spec;
  if (run.mode === 'consult') {
    $('#tab-overview').innerHTML = window.consultationCard?.() || ''; renderLive(); return;
  }
  if (run.mode === 'learn') {
    $("#tab-overview").innerHTML = `${window.learningCard?.() || ''}${window.teamResearchCard?.() || ''}<div class="card"><h3>研究任务</h3><p>${esc(run.brief)}</p><p class="small muted">来源：${esc([...(run.options.learning_sources || []).map(s=>s.title), ...(run.options.learning_runs || [])].join(' · ') || 'Agent 参考资料与本次研究要求')}</p></div><div class="card"><h3>研究实时输出<span class="sp small muted" id="liveMeta"></span></h3><div class="live" id="live"></div></div>`;
    renderLive(); return;
  }
  const specHtml = spec ? `
    <div class="kv"><b>游戏</b><span>${esc(spec.title || "")}</span>${spec.pitch ? `<b>卖点</b><span>${esc(spec.pitch)}</span>` : ""}
    ${spec.genre ? `<b>类型</b><span>${esc(spec.genre)}</span>` : ""}${spec.camera ? `<b>镜头</b><span>${esc(spec.camera)}</span>` : ""}
    ${spec.controls ? `<b>操作</b><span>${esc(spec.controls)}</span>` : ""}${spec.core_loop ? `<b>核心循环</b><span>${esc(spec.core_loop)}</span>` : ""}
    <b>美术风格</b><span>${esc(spec.art_style || "")}</span></div>
    <table style="margin-top:10px"><tr><th>资产</th><th>角色</th><th>尺寸 (m)</th><th>面数预算</th><th>建模</th></tr>
    ${spec.assets.map((a) => { const m = state.models?.[a.id]; return `<tr><td><b>${esc(a.name)}</b><div class="small muted">${esc(a.description)}</div></td><td>${esc(a.role)}</td><td>${(a.size_m || []).join("×")}</td><td>${a.poly_budget}</td><td>${m?.glb ? `已导出 · ${fmtScore(m.final_score)}` : m?.rounds?.length ? `第 ${m.rounds.length} 轮` : "—"}</td></tr>`; }).join("")}</table>
    ${spec.acceptance?.length ? `<div class="small" style="margin-top:10px"><b>验收标准</b><ul class="diffs">${spec.acceptance.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}` : `<div class="muted">方案规划中…</div>`;
  const refs = S.detail.artifacts.filter((a) => a.kind === "reference");
  $("#tab-overview").innerHTML = `${window.learningCard?.() || ''}
    ${window.assemblyRunCard?.() || ''}
    ${window.teamRunCard?.() || ''}
    <div class="grid2">
      <div>
        <div class="card"><h3>需求</h3><div>${esc(run.brief)}</div>${refs.length ? `<div class="thumbs" style="margin-top:10px">${refs.map((a) => thumb(artUrl(a.id), a.label, "sm")).join("")}</div>` : ""}</div>
        ${decomposeCard()}
        ${run.mode === 'atlas' ? window.atlasOverviewCard() : `<div class="card"><h3>方案</h3>${specHtml}</div>`}
      </div>
      <div>
        <div class="card"><h3>模型实时输出<span class="sp small muted" id="liveMeta"></span></h3>${latestIteration()}<div class="live" id="live"></div></div>
        ${routerCard()}
        <div class="card"><h3>给正在运行的 agent 追加指示</h3>
          <div class="row"><input id="noteInput" value="${esc(S.noteDraft || '')}" placeholder="例如：屋顶颜色改成深绿色；玩家移动速度再快一些"><button onclick="sendNote()" ${['running','waiting','paused'].includes(run.status) ? '' : 'disabled'}>发送</button></div>
          <div class="small muted" style="margin-top:6px">指示会在 agent 的下一步送达模型。</div></div>
        <div class="card"><h3>最近验收</h3>${recentReviews()}</div>
      </div>
    </div>`;
  renderLive();
  bindThumbs($("#tab-overview"));
  $$(".live-img", $("#tab-overview")).forEach((el) => el.addEventListener("click", () => { $("#lightboxImg").src = el.dataset.full; $("#lightbox").showModal(); }));
}

function fmtScore(v) { return typeof v === "number" ? v.toFixed(v < 1 ? 3 : 1) : (v ?? ""); }
function oskarCard(state) { return oskarCardFor(state.oskar, "oskar_scene", "Oskar 风格场景"); }
function oskarCardFor(o, asset, heading) {
  const hist = (o.history || []).slice().reverse();
  const glb = (S.detail.artifacts || []).filter((x) => x.kind === "glb" && (x.meta?.asset || asset) === asset).pop();
  const prog = o.program || {};
  const dino = o.metrics?.dino;
  return `<div class="card"><h3>${heading} · ${esc(prog.scene || "")}<span class="sp small muted">${o.glb || glb ? `已导出 · 最佳第 ${o.best_round} 轮 · DINO ${fmtScore(dino)}` : hist.length ? `进行中 · 第 ${hist.length} 轮` : "规划中"}</span></h3>
    <div class="grid2"><div><div class="small muted">目标图</div><div class="thumbs">${thumb(fileUrl(o.target), "目标")}</div>
      <div class="small muted" style="margin-top:6px">场景程序：${(prog.items || []).length} 个原语（${esc([...new Set((prog.items || []).map((x) => x.type))].join(" / "))}）</div></div>
      <div>${glb ? `<div class="small muted">GLB 预览 · ${dlLink(glb.id)}</div><model-viewer src="${artUrl(glb.id)}" camera-controls auto-rotate shadow-intensity="1" exposure="1.1"></model-viewer>` : ""}</div></div>
    ${o.godot ? `<div class="round"><div><div class="small muted">Godot</div><div class="score ${o.godot.ok ? "pass" : "fail"}">${o.godot.ok ? "通过" : "未过"}</div>
        ${o.godot.fps ? `<div class="small muted">${Math.round(o.godot.fps)} FPS</div>` : ""}</div>
      <div><div class="thumbs">${(o.godot.shots || []).map((p, i) => thumb(fileUrl(p), (o.godot.labels || [])[i] || "", "sm")).join("")}</div>
        ${(o.godot.checks || []).map((c) => `<div class="small" style="color:${c.passed ? "var(--ok)" : "var(--warn)"}">${esc(c.name)}：${esc(c.detail)}</div>`).join("")}
        <div class="small muted">工程：${esc(o.godot.project)} · 拖动/A D 旋转，滚轮/W S 缩放，E 切换日光/黄昏</div></div></div>` : ""}
    ${hist.map((h) => { const fails = (h.checks || []).filter((c) => c.required && !c.passed);
      return `<div class="round"><div><div class="small muted">第 ${h.round} 轮</div><div class="score ${h.accepted ? "pass" : "fail"}">${h.metrics ? h.metrics.dino.toFixed(3) : "—"}</div>
        <div class="small ${h.accepted ? "" : "muted"}">${h.round === 0 ? "初稿" : h.error ? "出错" : h.accepted ? "接受" : "回滚"}</div>${o.best_round === h.round ? `<div class="small" style="color:var(--ok)">最佳</div>` : ""}</div>
        <div><div class="thumbs">${(h.images || []).map((p) => thumb(fileUrl(p), (p.includes("hero_left") ? "左侧机位" : "正面机位"), "sm")).join("")}</div>
          ${h.metrics ? `<div class="small">客观相似度 DINO ${h.metrics.dino.toFixed(3)} / SigLIP ${h.metrics.siglip.toFixed(3)}</div>` : ""}
          <div class="small" style="color:${fails.length ? "var(--warn)" : "var(--ok)"}">硬检查：${fails.length ? esc(fails.map((c) => `${c.name} ${c.detail}`).join("；")) : "全部通过"}</div>
          ${h.diffs?.length ? `<ul class="diffs">${h.diffs.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
          ${h.edit_log?.length ? `<div class="small muted">编辑：${esc(h.edit_log.join("，"))}</div>` : ""}
          ${h.notes?.length ? `<div class="small muted">引擎自动修正：${esc(h.notes.join("；"))}</div>` : ""}
          ${h.error ? `<div class="small" style="color:var(--warn)">${esc(h.error)}</div>` : ""}</div></div>`; }).join("")}
  </div>`;
}
function focusAsset(id) { S.modelFocus = id; setTab("model"); main().scrollTop = 0; }
window.focusAsset = focusAsset;

function assetPills(spec, models) {
  const pill = (id, label, extra, on) => `<button class="pill ${on ? "on" : ""}" onclick="focusAsset(${id ? `'${esc(id)}'` : "null"})">${esc(label)}${extra ? ` <span class="muted small">${extra}</span>` : ""}</button>`;
  const items = spec.assets.map((a) => {
    const m = models[a.id]; const b = m && m.best != null ? m.rounds[m.best] : null;
    const st = m?.glb ? `已导出 ${b ? b.score.toFixed(1) : ""}` : m?.rounds?.length ? `第${m.rounds.length}轮 ${b ? b.score.toFixed(1) : ""}` : "待建模";
    return pill(a.id, a.name, st, S.modelFocus === a.id);
  });
  if (S.detail.state.compose) items.push(pill("_compose", "场景组装", "", S.modelFocus === "_compose"));
  return `<div class="pills">${pill(null, "全部", "", !S.modelFocus)}${items.join("")}</div>`;
}

function routerCard() {
  const cfg = S.status?.router || {};
  const used = S.detail.state._routes || {};
  const svc = Object.fromEntries((S.status?.services || []).map((x) => [x.name.replace("llm-", ""), x.label.split(" (")[0]]));
  const rows = Object.entries(cfg).map(([role, v]) => {
    const m = used[role] || v.model;
    return `<tr><td>${esc(v.label)}</td><td>${esc(svc[m] || m)}${used[role] ? "" : ` <span class="muted small">（配置）</span>`}</td></tr>`;
  }).join("");
  return rows ? `<div class="card"><h3>LLM 路由<span class="sp small muted">子任务 → 模型，产出均经验证</span></h3><table>${rows}</table></div>` : "";
}

function decomposeCard() {
  const a = S.detail.artifacts.find((x) => x.kind === "decompose");
  if (!a) return "";
  const spec = S.detail.state.spec || {};
  return `<div class="card"><h3>复杂任务拆分<span class="sp small muted">${(spec.assets || []).length} 种子资产 · ${(spec.assets || []).reduce((n, x) => n + (x.placements || []).length, 0)} 个实例</span></h3>
    <div class="thumbs">${thumb(artUrl(a.id), "框 = 子资产在目标图中的裁剪区域", "wide")}</div>
    <table style="margin-top:8px"><tr><th>#</th><th>子资产</th><th>尺寸 (m)</th><th>实例</th><th>建模</th></tr>${(spec.assets || []).map((x, i) => { const m = S.detail.state.models?.[x.id]; const b = m && m.best != null ? m.rounds[m.best] : null; return `<tr class="clickable" onclick="focusAsset('${esc(x.id)}')" title="查看该子资产的建模过程"><td>${i + 1}</td><td><b>${esc(x.name)}</b> <span class="muted small">${esc(x.id)}</span></td><td>${(x.size_m || []).join("×")}</td><td>${(x.placements || []).length}</td><td>${m?.glb ? `已导出 ${b ? b.score.toFixed(1) : ""}` : m?.rounds?.length ? `第 ${m.rounds.length} 轮 · 最佳 ${b ? b.score.toFixed(1) : "-"}` : "待建模"}</td></tr>`; }).join("")}</table>
    ${S.detail.state.compose ? `<div class="small" style="margin-top:6px">场景组装：${S.detail.state.compose.rounds.length} 轮，冠军 ${(() => { const c = S.detail.state.compose; const r = c.rounds[c.champion]; return r ? `第 ${r.round} 轮 ${r.score.toFixed(1)} 分` : "-"; })()}${S.detail.state.compose.glb ? " · 已导出" : ""}</div>` : ""}
  </div>`;
}

// 最新一次迭代的图：优先“目标|当前”对比图，其次最新渲染
function latestIteration() {
  const arts = S.detail.artifacts;
  const cmp = [...arts].reverse().find((a) => a.kind === "compare");
  const rnd = [...arts].reverse().find((a) => a.kind === "render" || a.kind === "screenshot");
  const pick = cmp && (!rnd || cmp.created >= rnd.created - 120) ? cmp : rnd;
  if (!pick) return "";
  const score = pick.meta?.score != null ? ` · ${(+pick.meta.score).toFixed(1)} 分` : "";
  return `<div class="live-img" data-full="${artUrl(pick.id)}"><img src="${artUrl(pick.id)}" alt=""><div class="small muted">${esc(pick.label)}${score} · ${fmtTime(pick.created)}（点击放大）</div></div>`;
}

function recentReviews() {
  const revs = S.events.filter((e) => e.kind === "review").slice(-5).reverse();
  if (!revs.length) return `<div class="muted small">暂无</div>`;
  return revs.map((e) => `<div style="margin-bottom:8px"><div>${esc(e.message)}</div>${e.data?.diffs?.length ? `<ul class="diffs">${e.data.diffs.slice(0, 3).map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}</div>`).join("");
}

function renderLive() {
  const el = $("#live"); if (!el) return;
  el.innerHTML = S.live.slice(-60).map((x) => `<span class="${x.kind === "reasoning" ? "r" : "c"}">${esc(x.text)}</span>`).join("") || `<span class="muted">等待模型输出…（灰色为思考过程）</span>`;
  el.scrollTop = el.scrollHeight;
}

function thumb(url, cap, cls = "", extra = "") {
  return `<div class="thumb ${cls}" data-full="${url}">${extra}<img loading="lazy" src="${url}" alt="">${cap ? `<div class="cap">${cap}</div>` : ""}</div>`;
}
function bindThumbs(root) {
  $$(".thumb", root).forEach((t) => t.addEventListener("click", (ev) => {
    if (ev.target.closest(".pick")) return;
    $("#lightboxImg").src = t.dataset.full; $("#lightbox").showModal();
  }));
}

function renderConcept() {
  const { state, run, artifacts } = S.detail; const spec = state.spec; const concepts = state.concepts || {};
  if (!spec) { $("#tab-concept").innerHTML = `<div class="muted">等待方案规划完成</div>`; return; }
  const waiting = run.status === "waiting";
  const ids = [...(concepts._scene ? [["_scene", "场景目标图"]] : []), ...spec.assets.map((a) => [a.id, a.name]), ...(concepts._key_art ? [["_key_art", "关键画面（场景）"]] : [])];
  const artById = Object.fromEntries(artifacts.map((a) => [a.id, a]));
  $("#tab-concept").innerHTML = `<div class="card">
    ${waiting ? `<div class="banner"><span>点击候选图的「选这张」可更换目标图；输入新提示词可重新生成。</span><button class="primary" onclick="approve()">确认并继续</button></div>` : ""}
    ${ids.map(([id, name]) => {
      const c = concepts[id]; const cands = c?.candidates || [];
      const selIdx = S.pendingSel[id] ?? c?.selected;
      return `<div class="asset-block"><h3>${esc(name)} <span class="small muted">${esc(id)}</span></h3>
        <div class="thumbs">${cands.map((cd, i) => thumb(artUrl(cd.artifact), `#${i}${cd.oskar ? ` · Oskar ${cd.oskar.director.toFixed(1)}${cd.oskar.buildable ? "" : "（难建）"}` : ""}<span>${artById[cd.artifact]?.meta?.seconds ? artById[cd.artifact].meta.seconds + "s" : ""}</span>`, i === selIdx ? "sel" : "", waiting ? `<button class="pick" data-asset="${id}" data-art="${cd.artifact}" data-idx="${i}">${i === selIdx ? "✓ 已选" : "选这张"}</button>` : "")).join("") || `<span class="muted small">生成中…</span>`}</div>
        ${waiting ? `<div class="row" style="margin-top:8px"><input data-prompt="${id}" value="${esc(c?.prompt || "")}"><button data-regen="${id}">重新生成 2 张</button></div>` : `<div class="small muted" style="margin-top:6px">${esc(c?.prompt || "")}</div>`}
      </div>`;
    }).join("")}</div>`;
  $$(".pick").forEach((b) => (b.onclick = () => { S.pendingSel[b.dataset.asset] = +b.dataset.idx; S.pendingSel["__art_" + b.dataset.asset] = b.dataset.art; renderConcept(); }));
  $$("[data-regen]").forEach((b) => (b.onclick = async () => { b.disabled = true; b.textContent = "已提交…"; await post(`/api/runs/${S.runId}/regenerate`, { asset: b.dataset.regen, prompt: $(`[data-prompt="${b.dataset.regen}"]`).value, count: 2 }); }));
  bindThumbs($("#tab-concept"));
}

function renderModel() {
  const { state } = S.detail; const spec = state.spec; const models = state.models || {};
  if (!spec) { $("#tab-model").innerHTML = `<div class="muted">等待方案规划完成</div>`; return; }
  const glbs = Object.fromEntries(S.detail.artifacts.filter((a) => a.kind === "glb").map((a) => [a.meta.asset, a]));
  const focus = S.modelFocus;
  const shown = focus && focus !== "_compose" ? spec.assets.filter((a) => a.id === focus) : focus === "_compose" ? [] : spec.assets;
  if (state.oskar) { $("#tab-model").innerHTML = oskarCard(state); bindThumbs($("#tab-model")); return; }
  if (state.atlas) { $("#tab-model").innerHTML = window.atlasModelView(); bindThumbs($("#tab-model")); return; }
  $("#tab-model").innerHTML = (state.world ? oskarCardFor(state.world, "oskar_world", "Oskar 世界") : "") +
    (spec.assets.length > 1 ? assetPills(spec, models) : "") + shown.map((a) => {
    const m = models[a.id]; const c = state.concepts?.[a.id];
    const target = c ? c.candidates[c.selected ?? 0]?.path : null;
    const rounds = (m?.rounds || []).slice().reverse();
    const glb = glbs[a.id];
    const fc = m?.forecast;
    return `<div class="card"><h3>${esc(a.name)}<span class="sp small muted">${m?.glb ? `已导出 · 最终 ${fmtScore(m.final_score)}` : rounds.length ? `进行中 · 第 ${rounds.length} 轮` : "未开始"}</span></h3>
      ${fc ? `<div class="small" style="margin-bottom:6px">收敛预测：冠军 <b>${fc.champion_score.toFixed(1)}</b> 分，距通过线 ${fc.gap} · ${fc.eta_rounds ? `按最近 ${fc.window} 轮速度（+${fc.rate}/轮）预计还需约 <b>${fc.eta_rounds}</b> 轮` : `已 ${fc.stalled} 轮未进步（停滞）`} · 剩余明显/严重差异 ${fc.open_severe} 条</div>` : ""}
      <div class="grid2">
        <div><div class="small muted">目标概念图</div>${target ? `<div class="thumbs">${thumb(fileUrl(target), "目标")}</div>` : "—"}</div>
        <div>${glb ? `<div class="small muted">GLB 预览（可拖动旋转） · ${glb.meta.triangles ?? "?"} 三角面 · 尺寸 ${(glb.meta.size || []).join("×")} m · ${dlLink(glb.id)}</div><model-viewer src="${artUrl(glb.id)}" camera-controls auto-rotate shadow-intensity="1" exposure="1.1"></model-viewer>` : `<div class="small muted">导出后可在此 3D 预览</div>`}</div>
      </div>
      ${rounds.map((r) => `<div class="round"><div><div class="small muted">第 ${r.round} 轮</div><div class="score ${r.review.pass ? "pass" : "fail"}">${r.score.toFixed(1)}</div>${m.best === r.round - 1 ? `<div class="small" style="color:var(--ok)">最佳</div>` : ""}</div>
        <div><div class="thumbs">${r.renders.map((p) => thumb(fileUrl(p), p.split("_").pop().replace(".png", ""), "sm")).join("")}</div>
        ${r.hard?.length ? `<div class="small" style="color:var(--warn);margin-top:6px">硬性检查：${esc(r.hard.join("；"))}</div>` : ""}
        ${r.observation?.compare ? `<div class="thumbs" style="margin-top:6px">${thumb(fileUrl(r.observation.compare), "目标 | 当前 对比", "wide")}${r.verdict?.compare ? thumb(fileUrl(r.verdict.compare), "成对比较 A|B", "wide") : ""}</div>` : ""}
        ${r.verdict ? `<div class="small" style="margin-top:4px">成对比较：<b>${r.verdict.closer === "B" ? "本轮更接近目标" : r.verdict.closer === "A" ? "上一版更好" : "差不多"}</b> · 焦点 ${esc(Object.entries(r.verdict.focus || {}).map(([k, v]) => `${k}:${{improved: "改善", same: "持平", worse: "变差"}[v] || v}`).join("，"))}${r.verdict.new_problems?.length ? ` · 新问题：${esc(r.verdict.new_problems.join("；"))}` : ""}</div>` : ""}
        ${m.champion === (m.rounds || []).indexOf(r) ? `<div class="small" style="color:var(--ok)">当前冠军</div>` : ""}
        ${r.review.diffs?.length ? `<ul class="diffs">${r.review.diffs.slice(0, 8).map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
        ${r.decision ? `<div class="small" style="margin-top:4px">System One（${esc({laya: "Laya", "llm-review": "LLM 复核", severity: "按严重度"}[r.decision.decider] || r.decision.decider || "")}）：<b>${esc(r.decision.next_step)}</b> · 置信度 ${r.decision.confidence ?? "?"} · 焦点 ${esc((r.decision.focus || []).join(", "))} · ${esc(r.decision.gate_reason || "")}<div class="muted">${esc(r.decision.plan || "")}</div></div>` : ""}
        <div class="small muted">${r.blueprint ? `${r.blueprint_path ? `<a href="${fileUrl(r.blueprint_path)}" target="_blank">蓝图 JSON</a> · ` : ""}蓝图：${r.build?.built ?? "?"} 个部件${r.build?.errors?.length ? `，${r.build.errors.length} 个构建错误` : ""} / ${r.agent.seconds}s` : `agent：${r.agent.steps} 步 / ${r.agent.calls} 次工具调用 / ${r.agent.seconds}s`} — ${esc((r.agent.summary || "").slice(0, 160))}</div></div></div>`).join("")}
    </div>`;
  }).join("") + (!focus || focus === "_compose" ? composeCard() : "");
  bindThumbs($("#tab-model"));
}

function composeCard() {
  const c = S.detail.state.compose;
  if (!c) return "";
  const glb = S.detail.artifacts.find((a) => a.kind === "glb" && a.meta.asset === "_scene");
  return `<div class="card"><h3>场景组装（布局迭代）<span class="sp small muted">${c.rounds.length} 轮 · 冠军 ${c.champion != null ? "第 " + c.rounds[c.champion].round + " 轮" : "-"}</span></h3>
    ${glb ? `<model-viewer src="${artUrl(glb.id)}" camera-controls auto-rotate shadow-intensity="1"></model-viewer>` : ""}
    ${c.rounds.slice().reverse().map((r) => `<div class="round"><div><div class="small muted">第 ${r.round} 轮</div><div class="score ${r.decision ? "fail" : "pass"}">${r.score.toFixed(1)}</div>${c.rounds[c.champion] === r ? `<div class="small" style="color:var(--ok)">冠军</div>` : ""}</div>
      <div><div class="thumbs">${thumb(fileUrl(r.observation.compare), "目标 | 组装结果", "wide")}${r.verdict?.compare ? thumb(fileUrl(r.verdict.compare), "成对比较", "wide") : ""}</div>
      <ul class="diffs">${r.observation.diffs.slice(0, 6).map((d) => `<li>[${esc(d.id)} ${esc(d.part)}·S${d.severity}] ${esc(d.observation)}</li>`).join("")}</ul>
      ${r.decision ? `<div class="small">System One：<b>${esc(r.decision.next_step)}</b> · ${esc(r.decision.gate_reason || "")}</div>` : ""}
      <div class="small muted">${r.layout.length} 个实例 · ${r.seconds}s</div></div></div>`).join("")}
  </div>`;
}

function renderGame() {
  const { state, run } = S.detail;
  if (!["game", "assembly", "atlas"].includes(run.mode)) { $("#tab-game").innerHTML = `<div class="muted">该任务不包含游戏组装；场景预览请查看「资产与场景」。</div>`; return; }
  const as = state.assemble?.rounds || []; const fr = state.final_review;
  const key = state.concepts?._key_art; const keyPath = key ? key.candidates[key.selected ?? 0]?.path : null;
  $("#tab-game").innerHTML = `
    <div class="card"><h3>游戏<span class="sp row"><button onclick="openGodot(false)">▶ 运行游戏</button><button onclick="openGodot(true)">在 Godot 编辑器中打开</button></span></h3>
      <div class="grid2"><div><div class="small muted">关键画面目标</div>${keyPath ? `<div class="thumbs">${thumb(fileUrl(keyPath), "目标", "wide")}</div>` : "—"}</div>
      <div>${fr ? `<div class="small muted">最终验收</div><div class="score ${fr.review.pass ? "pass" : "fail"}">${fr.review.score.toFixed(1)} / 10</div><div class="small">${esc(fr.test.facts)}</div><div class="thumbs" style="margin-top:8px">${fr.images.map((p) => thumb(fileUrl(p), "", "wide")).join("")}</div>` : `<div class="muted small">尚未最终验收</div>`}</div></div></div>
    ${as.slice().reverse().map((r) => `<div class="card"><div class="round" style="border:none;padding:0"><div><div class="small muted">第 ${r.round} 轮</div><div class="score ${r.review.pass ? "pass" : "fail"}">${r.score.toFixed(1)}</div></div>
      <div><div class="small">${esc(r.test.facts)}</div><div class="thumbs" style="margin-top:6px">${(r.test.images || []).map((p) => thumb(fileUrl(p), p.split("shot_").pop().replace(".png", "s"), "wide")).join("")}</div>
      ${r.review.diffs?.length ? `<ul class="diffs">${r.review.diffs.slice(0, 5).map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
      <div class="small muted">${esc((r.agent.summary || "").slice(0, 200))}</div></div></div></div>`).join("")}`;
  bindThumbs($("#tab-game"));
}

function dlLink(id) {   // 可玩的任务：先试玩确认，再给下载
  return isPlayable() && !S.detail.run.options.playtest?.confirmed ? `<span class="muted">试玩确认后可下载</span>` : `<a href="${artUrl(id)}" download>下载</a>`;
}
function isPlayable() {
  const { run, state } = S.detail; const st = state || {};
  return ["game", "assembly", "atlas", "oskar"].includes(run.mode) &&
    !!(st.planet?.godot?.ok || st.atlas?.godot?.ok || st.oskar?.godot?.ok || st.final_review?.test?.hard_ok === true);
}
async function confirmPlaytest() {
  try { await post(`/api/runs/${S.runId}/playtested`, {}); toast("已确认试玩：下载与发布已开放"); await selectRun(S.runId); setTab("deliver"); } catch (e) { toast(e.message); }
}
async function publishRun() {
  try { await post(`/api/runs/${S.runId}/publish`, {}); toast("正在发布：打包可玩工程，并在本机有导出模板时导出 macOS 应用与 Web 版本"); setTimeout(() => selectRun(S.runId), 1500); } catch (e) { toast(e.message); }
}
async function renderDeliver() {
  const runId = S.runId;
  if (isPlayable() && !S.detail.run.options.playtest?.confirmed) {
    // 先游玩测试，再提供下载与发布
    $("#tab-deliver").innerHTML = `<div class="card"><h3>先游玩测试，再下载与发布</h3><p class="small muted">进入游戏实际玩一遍：能走、能跳、能找到信、能坐电车、司机视角没有遮挡。确认后这里才会出现下载与发布。</p><div class="row"><button class="primary" onclick="openGodot(false)">▶ 进入游戏试玩</button><button onclick="confirmPlaytest()" data-mutate>我已试玩，确认可以发布</button></div></div>`;
    return;
  }
  const rel = S.detail.run.options.release;
  const arts = S.detail.artifacts.filter((a) => ["package", "report", "glb", "spec", "style_draft", "release"].includes(a.kind));
  const gate = S.detail.quality;
  const report = arts.find((a) => a.kind === "report");
  const publishCard = isPlayable() ? `<div class="card"><h3>发布<span class="sp row"><button class="primary" onclick="publishRun()" data-mutate ${rel?.status === "running" ? "disabled" : ""}>${rel?.status === "done" ? "重新发布" : rel?.status === "running" ? "发布中…" : "发布"}</button><button onclick="openGodot(false)">▶ 再玩一次</button></span></h3><p class="small muted">发布生成：可玩工程包（Godot 4 打开即玩）；本机装了 Godot 导出模板时，另导出 macOS 应用与 Web 版本。</p>${rel?.notes?.length ? `<ul class="diffs">${rel.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}${rel?.status === "done" ? `<div class="small">已发布：${esc((rel.files || []).join("、"))}</div>` : ""}</div>` : "";
  $("#tab-deliver").innerHTML = publishCard + `${gate?.status !== "passed" ? `<div class="notice">${esc(gate?.label || "未验证")}：下列文件为预览或历史产物，文件存在不代表验收通过。</div>` : ""}<div class="card"><h3>产物与文件</h3>${arts.length ? `<table>${arts.map((a) => `<tr><td>${esc(a.label)}</td><td class="muted small">${esc(a.kind)}</td><td><a href="${artUrl(a.id)}" download>下载</a></td></tr>`).join("")}</table>` : `<div class="muted small">交付阶段完成后出现</div>`}</div>
    ${report ? `<div class="card"><h3>REPORT.md</h3><pre id="reportText">加载中…</pre></div>` : ""}`;
  if (report) { const text = await api(artUrl(report.id)); if (S.runId === runId && $("#reportText")) $("#reportText").textContent = text; }
}

// ───────── 日志 ─────────
const LOG_GROUPS = { tool: ["tool_call", "tool_result", "tool_error", "images"], review: ["review", "result", "approval", "approved"], compute: ["compute"], error: ["error", "tool_error", "warn", "fallback"], llm: ["llm", "finish", "note"] };
function addEvent(e, live = true) {
  if (e.id && e.id <= S.lastEventId) return;
  if (e.id) S.lastEventId = e.id;
  if (e.kind === "stream") { S.live.push(e.data); if (S.live.length > 400) S.live.splice(0, 200); if (live) renderLive(); return; }
  S.events.push(e);
  if (S.events.length > 3000) S.events.splice(0, 1000);
  if (live && S.tab === "log") appendLogRow(e);
}
function logVisible(e) { return S.logFilter === "all" || (LOG_GROUPS[S.logFilter] || []).includes(e.kind); }
function logRow(e) {
  let body = esc(e.message);
  if (e.data && ["tool_call", "tool_result", "tool_error", "llm", "error", "review", "result", "images", "compute"].includes(e.kind)) {
    const d = e.kind === "tool_call" && e.data.args?.code ? e.data.args.code : JSON.stringify(e.data, null, 1);
    body = `<details><summary>${esc(e.message) || e.kind}</summary><pre>${esc(d)}</pre></details>`;
    if (e.kind === "images" && e.data.images) body += `<div class="thumbs" style="margin-top:4px">${e.data.images.map((p) => thumb(fileUrl(p), "", "sm")).join("")}</div>`;
  }
  return `<div class="ev ${e.kind}"><span class="ts">${fmtTime(e.ts)}</span><span class="k">${esc(e.kind)}</span><div>${body}</div></div>`;
}
function renderLog() {
  if (S.tab !== "log") return;
  const el = $("#log");
  el.innerHTML = S.events.filter(logVisible).slice(-600).map(logRow).join("");
  bindThumbs(el);
}
function appendLogRow(e) {
  if (!logVisible(e)) return;
  const el = $("#log"); const nearBottom = main().scrollHeight - main().scrollTop - main().clientHeight < 200;
  el.insertAdjacentHTML("beforeend", logRow(e)); bindThumbs(el.lastElementChild);
  if (nearBottom) main().scrollTop = main().scrollHeight;
}
const main = () => $("main");

// ───────── 控制 ─────────
async function approve(gate = "concept") {
  const selections = {};
  for (const [k, v] of Object.entries(S.pendingSel)) if (k.startsWith("__art_")) selections[k.slice(6)] = v;
  await post(`/api/runs/${S.runId}/approve`, { gate, selections });
}
async function retry() { await post(`/api/runs/${S.runId}/retry`, { from_stage: $("#retrySel").value }); refreshSoon(); }
async function sendNote() { const v = $("#noteInput").value.trim(); if (!v) return; await post(`/api/runs/${S.runId}/note`, { text: v }); $("#noteInput").value = ""; S.noteDraft = ""; }
async function openGodot(editor) { try { await post(`/api/runs/${S.runId}/open-godot`, { editor }); window.toast?.(editor ? "正在用 Godot 编辑器打开…" : "游戏窗口正在本机打开（Godot 首次导入模型需要几秒）"); } catch (e) { window.toast?.(e.message); } }
async function playDemo() { try { await post(`/api/play/planet-demo`, {}); window.toast?.("星球样例正在本机打开（Godot 首次导入模型需要几秒）"); } catch (e) { window.toast?.(e.message); } }
window.approve = approve; window.retry = retry; window.sendNote = sendNote; window.openGodot = openGodot; window.setTab = setTab;

$("#pauseBtn").onclick = () => post(`/api/runs/${S.runId}/pause`).then(refreshSoon);
$("#resumeBtn").onclick = () => post(`/api/runs/${S.runId}/resume`).then(refreshSoon);
function openRunAction(kind) {
  if (!S.detail || S.status?.read_only || S.runAction?.busy) return;
  const run = S.detail.run;
  S.runAction = {kind, id:run.id, title:run.title || run.brief.slice(0,60), busy:false};
  const deleting = kind === 'delete';
  $('#runActionTitle').textContent = deleting ? '移除这次创作记录？' : '取消这次任务？';
  $('#runActionTarget').textContent = S.runAction.title;
  $('#runActionId').textContent = run.id;
  $('#runActionDescription').textContent = deleting
    ? '已有运行文件会移到 runs/.trash/，可手动找回；任务、日志与项目关联记录会从工作台移除。'
    : '排队任务将退出队列；正在执行的任务会在安全检查点停止。已保存的文件和日志保留，可以稍后重跑。';
  $('#runActionSubmit').textContent = deleting ? '移除记录并保留文件' : '确认取消任务';
  $('#runActionError').textContent = '';
  $('#runActionSubmit').disabled = false; $('#runActionKeep').disabled = false;
  $('#runActionDlg').showModal();
  $('#runActionKeep').focus();
}
$('#runActionKeep').onclick = () => $('#runActionDlg').close();
$('#runActionDlg').addEventListener('cancel', ev => { if (S.runAction?.busy) ev.preventDefault(); });
$('#runActionDlg').addEventListener('close', () => { if (!S.runAction?.busy) S.runAction = null; });
$('#runActionForm').onsubmit = async ev => {
  ev.preventDefault();
  const action = S.runAction;
  if (!action || action.busy || S.status?.read_only) return;
  action.busy = true;
  $('#runActionSubmit').disabled = true; $('#runActionKeep').disabled = true;
  $('#runActionError').textContent = '';
  try {
    if (action.kind === 'delete') await api(`/api/runs/${action.id}`, {method:'DELETE'});
    else await post(`/api/runs/${action.id}/cancel`);
  } catch (error) {
    action.busy = false;
    $('#runActionSubmit').disabled = false; $('#runActionKeep').disabled = false;
    $('#runActionError').textContent = error.message || '操作未完成，请重试。';
    return;
  }
  action.busy = false; $('#runActionDlg').close();
  if (action.kind === 'delete') {
    if (S.runId === action.id) window.showWorkspace?.();
    window.toast?.('任务记录已移除，已有运行文件保留在回收目录。');
  } else window.toast?.('已请求取消，执行中的任务将在安全检查点停止。');
  refreshSoon();
};
$("#cancelBtn").onclick = () => openRunAction('cancel');
$("#retryBtn").onclick = retry;
$("#deleteBtn").onclick = () => openRunAction('delete');
$("#revealBtn").onclick = () => post(`/api/runs/${S.runId}/reveal`);
$("#modelSel").onchange = (e) => post(`/api/runs/${S.runId}/model`, { model: e.target.value });
$$("#tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
$$("#logFilters button").forEach((b) => (b.onclick = () => { S.logFilter = b.dataset.f; $$("#logFilters button").forEach((x) => x.classList.toggle("on", x === b)); renderLog(); }));

// 新建运行
$("#newRunBtn").onclick = () => {
  const d = S.status?.defaults || {}; const f = $("#newRunForm");
  $("#projectSel").innerHTML = `<option value="">（不归属项目）</option>` + (S.projects || []).map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#projectSel").onchange = () => { const p = (S.projects || []).find((x) => x.id === $("#projectSel").value); if (p?.mode) { const r = f.querySelector(`input[name=mode][value=${p.mode}]`); if (r) r.checked = true; } };
  f.concept_candidates.placeholder = d.concept_candidates ?? 3; f.model_max_rounds.placeholder = d.model_max_rounds ?? 6;
  f.model_pass_score.placeholder = d.model_pass_score ?? 7.5; f.godot_max_rounds.placeholder = d.godot_max_rounds ?? 6;
  $("#newRunDlg").showModal();
};
$("#newRunCancel").onclick = () => $("#newRunDlg").close();
$("#newRunForm").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target; const fd = new FormData();
  fd.append("brief", f.brief.value); fd.append("mode", f.mode.value);
  const opts = { model: f.model.value, depth_layout: f.depth_model.value !== "off", depth_model: f.depth_model.value === "off" ? "da3" : f.depth_model.value, image_backend: f.image_backend.value, approval_gates: f.approval.value ? [f.approval.value] : [], style_agent: f.style_agent.value, learn_after_run: f.learn_after_run.checked };
  for (const k of ["concept_candidates", "max_assets", "model_max_rounds", "model_pass_score", "godot_max_rounds", "plateau_patience", "compose_max_rounds", "shape3d_candidates", "oskar_rounds", "atlas_scenes", "atlas_rounds"]) if (f[k].value) opts[k] = +f[k].value;
  if (f.atlas_form && f.atlas_form.value) opts.atlas_form = f.atlas_form.value;
  if (f.oskar_palette.value) opts.oskar_palette = f.oskar_palette.value;
  if (f.new_project.value.trim()) {
    const p = await post("/api/projects", { name: f.new_project.value.trim(), mode: f.mode.value });
    opts.project = p.id;
  } else if (f.project.value) opts.project = f.project.value;
  if (f.source_model.value.trim()) opts.source_model = f.source_model.value.trim();
  if (f.integrate_to.value.trim()) opts.integrate_to = f.integrate_to.value.trim();
  if (f.reference_as_target.checked) opts.reference_as_target = true;
  fd.append("options", JSON.stringify(opts));
  for (const file of f.references.files) fd.append("references", file);
  const r = await api("/api/runs", { method: "POST", body: fd });
  $("#newRunDlg").close(); f.reset();
  await loadRuns(); selectRun(r.id);
};

// 大图预览：点图片、点背景、点 ✕ 或按 Esc 都能关闭
(() => {
  const lb = $("#lightbox");
  const close = () => { if (lb.open) lb.close(); };
  lb.addEventListener("click", close);
  $(".lb-close", lb).addEventListener("click", (e) => { e.stopPropagation(); close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();

// ───────── 实时 ─────────
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => { loadDetail(); loadRuns(); }, 400); }
// 长阶段（组装/建模）大量 kind=stream 事件时也要周期性刷新详情，否则步进条与轮次会一直停在旧快照
let progressRefreshAt = 0;
function maybeRefreshProgress(force = false) {
  const now = Date.now();
  if (!force && now - progressRefreshAt < 12000) return;
  progressRefreshAt = now;
  if (typeof loadDetail === "function") loadDetail();
  if (typeof loadRuns === "function") loadRuns();
}
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "event" && msg.run_id === S.runId) {
      addEvent(msg);
      if (["review", "result", "stage_done", "approval", "round", "images"].includes(msg.kind)) refreshSoon();
      else if (msg.kind === "stream" || msg.kind === "log" || msg.kind === "status") maybeRefreshProgress();
      if (msg.kind === "compute") loadStatus();
    } else if (msg.type === "run" || msg.type === "stage") {
      if (msg.run_id === S.runId) refreshSoon(); else loadRuns();
      if (S.view === "agents") loadAgents();
    } else if ((msg.type === "artifact" || msg.type === "artifact_meta") && msg.run_id === S.runId) refreshSoon();
  };
  es.onerror = () => { es.close(); setTimeout(connect, 3000); };
}

// Startup is owned by workspace.js.
