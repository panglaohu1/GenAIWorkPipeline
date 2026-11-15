// bridge.mjs — 管线（Python）调用 TigerInBamboo 能力代码的桥。一次调用一个子进程：
//   stdin  {"op": "...", "roots": {"reacode": 能力库目录, "workshop": 系统工坊目录}, ...}
//   stdout {"ok": true, "result": ...} 或 {"ok": false, "error": "..."}
//   stderr 每个阶段一行心跳 {"heartbeat": true, "stage": "..."}（Python 侧的 Watchdog 用）
// 能力代码原样 import，不复制、不改写：
//   voxel   REACodeRefactoring/src/capabilities/registry.js + verify.js（体素 World：石拱、悬垂、海中石柱、隧道、灯塔…）
//   object  system-workshop/object/capabilities.js + verify.js（物件布局：同类尺寸变化、朝向变化、间距松弛）
//   gscene  system-workshop/gscene/capabilities.js + verify.js（材质色表：主色归并、整体色调、风格包）
//   planet  system-workshop/planet/capabilities.js（星球配方；本管线只读描述）
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const beat = (stage) => process.stderr.write(JSON.stringify({ heartbeat: true, stage }) + '\n');
const imp = (root, rel) => import(pathToFileURL(path.join(root, rel)).href);

async function readStdin() {
  let s = '';
  for await (const c of process.stdin) s += c;
  return JSON.parse(s || '{}');
}

/** 体素 World 的最小实现：能力代码只用 grid.cells / solid / rock / color / river（ops.js），
 *  验证里的导航图（navgraph.js）再用 cells / top / ground / isRock。 */
function makeWorld(d) {
  const cells = d.cells.map((c) => ({ id: c.id, n: c.n ?? c.edgeNeighbors.length, edgeNeighbors: c.edgeNeighbors, cx: c.cx, cz: c.cz }));
  const hi = (m) => (m === 0 ? 0 : 32 - Math.clz32(m));
  const idOf = (c) => (typeof c === 'number' ? c : c.id);
  const w = {
    grid: { cells }, cells, version: 0, riverNetwork: null,
    solid: Int32Array.from(d.solid), rock: Int32Array.from(d.rock),
    color: Int32Array.from(d.color ?? new Array(cells.length).fill(-1)),
    river: Uint8Array.from(d.river ?? new Array(cells.length).fill(0)),
  };
  w.top = (c) => hi(w.solid[idOf(c)]);
  w.ground = (c) => hi(w.rock[idOf(c)]);
  w.isRock = (c, lv) => lv >= 1 && ((w.rock[idOf(c)] >> (lv - 1)) & 1) === 1;
  w.isSolid = (c, lv) => lv >= 1 && ((w.solid[idOf(c)] >> (lv - 1)) & 1) === 1;
  return w;
}
const dumpWorld = (w) => ({ solid: [...w.solid], rock: [...w.rock], color: [...w.color], river: [...w.river] });
const allowedOf = (ctx) => (ctx && ctx.allowed ? { ...ctx, allowed: Uint8Array.from(ctx.allowed) } : ctx || {});

const OPS = {
  async describe(m, R) {
    const out = {};
    const tryLoad = async (key, fn) => { try { out[key] = await fn(); } catch (e) { out[key] = { error: String(e?.message || e) }; } };
    await tryLoad('voxel', async () => (await imp(R.reacode, 'registry.js')).describeCapabilities().map((x) => ({ ...x, tier: 'voxel' })));
    await tryLoad('object', async () => (await imp(R.workshop, 'object/capabilities.js')).describeObjectCapabilities());
    await tryLoad('gscene', async () => (await imp(R.workshop, 'gscene/capabilities.js')).describeGsceneCapabilities());
    await tryLoad('planet', async () => (await imp(R.workshop, 'planet/capabilities.js')).describePlanetCapabilities());
    return out;
  },
  // ---------------------------------------------------------------- 体素
  async 'voxel.scan'(m, R) {
    beat('扫描体素 World');
    const reg = await imp(R.reacode, 'registry.js');
    return reg.matchAll(makeWorld(m.world), allowedOf(m.ctx), { focusCells: m.focusCells || null, limit: m.limit || 12 });
  },
  async 'voxel.run'(m, R) {
    beat('执行体素能力计划');
    const reg = await imp(R.reacode, 'registry.js');
    const w = makeWorld(m.world);
    const report = reg.runPlan(w, m.steps, allowedOf(m.ctx), { focusCells: m.focusCells || null });
    return { report, world: dumpWorld(w) };
  },
  async 'voxel.verify'(m, R) {
    beat('验证：改动范围 / 建筑保留 / 承托 / 通行 / 几何量');
    const ver = await imp(R.reacode, 'verify.js');
    const opts = { ...(m.opts || {}) };
    if (opts.allowed) opts.allowed = Uint8Array.from(opts.allowed);
    const before = makeWorld(m.before), after = makeWorld({ ...m.before, ...m.after });
    return ver.verifyCandidate(before, after, opts);
  },
  // ---------------------------------------------------------------- 物件
  async 'object.scan'(m, R) { beat('扫描物件布局'); return (await imp(R.workshop, 'object/capabilities.js')).matchObjects(m.layout); },
  async 'object.run'(m, R) { beat('执行物件能力计划'); return (await imp(R.workshop, 'object/capabilities.js')).runObjectPlan(m.layout, m.steps); },
  async 'object.verify'(m, R) { beat('验证：数值 / 范围 / 重叠 / 出界'); return (await imp(R.workshop, 'object/verify.js')).verifyObjects(m.layout, m.edits, { steps: m.steps || [] }); },
  // ---------------------------------------------------------------- 材质色表
  async 'gscene.scan'(m, R) { beat('扫描材质色表'); return (await imp(R.workshop, 'gscene/capabilities.js')).matchGscene(m.materials); },
  async 'gscene.run'(m, R) { beat('执行场景风格能力'); return (await imp(R.workshop, 'gscene/capabilities.js')).runGscenePlan(m.materials, m.steps); },
  async 'gscene.verify'(m, R) { beat('验证：明度 / 层次 / 颜色数'); return (await imp(R.workshop, 'gscene/verify.js')).verifyGscene(m.materials, m.colors); },
};

const msg = await readStdin().catch((e) => ({ op: '__bad__', error: e.message }));
try {
  const fn = OPS[msg.op];
  if (!fn) throw new Error('未知操作：' + msg.op);
  const result = await fn(msg, msg.roots || {});
  process.stdout.write(JSON.stringify({ ok: true, result }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e?.stack || e?.message || e).slice(0, 2000) }));
}
