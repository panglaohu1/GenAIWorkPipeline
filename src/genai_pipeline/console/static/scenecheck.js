// 场景验收：以「渲染图 vs 目标图」为主，3D 预览按渲染机位作辅助。
// 数据源：运行目录下 _phase_evidence/scene_check.json（由管线写出），
// 找不到时回退到控制台登记的 reference / render / compare 产物。
(function () {
  const dirOf = (p) => p.replace(/\/[^/]*$/, "");

  function runRoot(arts) {
    const ref = arts.filter((a) => a.kind === "reference").pop();
    if (ref && ref.path) {
      const dir = dirOf(ref.path);          // <root>/references
      return dir.replace(/\/[^/]*$/, "");   // <root>
    }
    const rep = arts.find((a) => a.kind === "report" && /\/deliverables\/REPORT\.md$/.test(a.path));
    if (rep) return rep.path.replace(/\/deliverables\/REPORT\.md$/, "");
    const pkg = arts.filter((a) => a.kind === "package").pop();
    if (pkg) return dirOf(pkg.path);
    return null;
  }

  const newestBy = (arts, kind, n) =>
    [...arts].filter((a) => a.kind === kind).sort((a, b) => (b.created || 0) - (a.created || 0)).slice(0, n);

  function card({ title, hint, body }) {
    return `<div class="card"><h3>${title}${hint ? `<span class="sp small muted">${hint}</span>` : ""}</h3>${body}</div>`;
  }

  window.renderSceneCheck = function () {
    const el = $("#tab-scenecheck");
    if (!el || !S.detail) return;
    const { run } = S.detail;
    const arts = S.detail.artifacts || [];
    const root = runRoot(arts);

    el.innerHTML = `<div class="muted">正在读取场景…</div>`;

    const fallback = () => {
      const renders = newestBy(arts, "render", 4);
      const compares = newestBy(arts, "compare", 2);
      const refs = arts.filter((a) => a.kind === "reference");
      const tgt = refs.length ? refs[refs.length - 1] : null;
      const rows = [];
      if (tgt) rows.push(`<div class="sc-pane"><div class="sc-cap">目标图</div><img src="${fileUrl(tgt.path)}" alt="目标图"></div>`);
      if (renders.length) rows.push(`<div class="sc-pane"><div class="sc-cap">最新渲染</div><img src="${artUrl(renders[0].id)}" alt="渲染"></div>`);
      return rows.length
        ? card({ title: "场景验收", hint: "目标图 × 最新渲染", body: `<div class="sc-wrap">${rows.join("")}</div>` })
        : card({ title: "场景验收", body: `<div class="muted small">这次运行还没有可对照的渲染图。</div>` });
    };

    if (!root) { el.innerHTML = fallback(); return; }

    const manifestUrl = fileUrl(`${root}/_phase_evidence/scene_check.json`);
    fetch(manifestUrl).then((r) => r.ok ? r.json() : null).catch(() => null).then((m) => {
      if (!m) { el.innerHTML = fallback(); return; }

      const state = S.detail.state || {};
      const comp = state.compose || {};
      let champ = null;
      if (comp.rounds && comp.rounds.length && comp.champion != null && comp.rounds[comp.champion]) {
        const r = comp.rounds[comp.champion];
        champ = { round: r.round, score: r.score };
      }
      const q = S.detail.quality || {};
      const when = (p) => {
        const f = (p || "").split("/").pop();
        return f || "";
      };

      const head = card({
        title: "场景验收",
        hint: run.title || "",
        body: `<div class="small muted">左边目标图、右边这一版渲出来的画面，先看像不像；下面的 3D 场景按同一个正交机位摆放，可切到自由旋转检查结构。${
          champ ? ` 组装冠军：第 ${champ.round} 轮，评分 ${Number(champ.score).toFixed(2)}/10。` : ""
        }</div>
        <div class="small muted" style="margin-top:6px">验收状态：${esc(q.label || "未记录")}${m.scene ? `　场景：${esc(when(m.scene))}` : ""}</div>`,
      });

      const main = m.render || m.compare ? `<div class="card">
        <h3>画面<span class="sp small muted">目标图 ／ 本次渲染</span></h3>
        <div class="sc-wrap">
          <div class="sc-pane"><div class="sc-cap">目标图</div>
            ${m.target ? `<img src="${fileUrl(m.target)}" alt="目标图" data-full="${fileUrl(m.target)}" class="sc-zoom">` : `<div class="muted small">没有目标图</div>`}
          </div>
          <div class="sc-pane"><div class="sc-cap">本次渲染</div>
            ${m.render ? `<img src="${fileUrl(m.render)}" alt="渲染" data-full="${fileUrl(m.render)}" class="sc-zoom">` : `<div class="muted small">没有渲染图</div>`}
          </div>
        </div>
        <div class="small muted" style="margin-top:8px">${esc(m.notes || "")}</div>
      </div>` : "";

      const viewer = m.scene ? `<div class="card">
        <h3>3D 场景<span class="sp small muted">默认按渲染机位，切「自由旋转」可转着看结构</span></h3>
        <iframe class="sc-viewer" src="/static/scenecheck/viewer.html?src=${encodeURIComponent(fileUrl(m.scene))}" allow="fullscreen"></iframe>
      </div>` : "";

      const extra = [];
      if (m.compare) extra.push(`<figure class="sc-thumb wide"><img src="${fileUrl(m.compare)}" alt="三联对比" data-full="${fileUrl(m.compare)}" class="sc-zoom"><figcaption>目标 ／ 新版 ／ 基线 三联对比</figcaption></figure>`);
      if (m.blooms) extra.push(`<figure class="sc-thumb"><img src="${fileUrl(m.blooms)}" alt="花特写" data-full="${fileUrl(m.blooms)}" class="sc-zoom"><figcaption>花丛 1:1 特写</figcaption></figure>`);
      const more = extra.length ? card({ title: "对照图", hint: "点开看大图", body: `<div class="sc-grid">${extra.join("")}</div>` }) : "";

      el.innerHTML = head + main + viewer + more;

      $$(".sc-zoom", el).forEach((img) => img.addEventListener("click", () => {
        $("#lightboxImg").src = img.dataset.full;
        $("#lightbox").showModal();
      }));
    });
  };
})();
