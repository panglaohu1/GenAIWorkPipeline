/**
 * lake-scene.js — 把一个平面湖区铺进 REACodeRefactoring 的 World 体素场。
 *
 * 这里只做坐标与地形适配，不复制任何建筑几何：点击长楼、叠层、削层、屋顶、门窗、
 * 支撑、撤销、存档全部由 engines/REACodeRefactoring 自己的管线提供
 * （core/world.js + build/buildings.js + world/view.js + ui/game-ui-c06.js）。
 *
 * 湖面在 World 里的表示就是"没有岩石的格子"（ground = 0 = 水面），
 * 所以点第一下水格就等于往水面放了一节楼 —— 和 Townscaper 完全一致。
 */

const MAX_SHORE_LEVEL = 3;

/* ---- 尺度：REA 建筑管线是按"一格 = 0.5、一层 = 0.5"设计的 ---------------
 * build/buildings.js 里窗口高 .095、披檐 0.085、屋顶厚度等都是绝对数，只有把
 * 网格和层高同时保持在 0.5，房子才是它本来的比例（1 格宽 ≈ 1 层高）。
 * 所以湖区网格用 radius 5 / hexSize 1.0（格距 0.5、318 格），
 * 落回球面时再整体乘 7.2，换回 3.6 m 一格的米制。 */
export const LAKE_CELL_METRES = 3.6;            // 湖区一格的真实宽度
export const LAKE_UNITS_PER_METRE = 0.5 / LAKE_CELL_METRES;   // 1 m = 1/7.2 引擎单位
export const LAKE_METRES_PER_UNIT = LAKE_CELL_METRES / 0.5;   // 1 引擎单位 = 7.2 m
export const LAKE_GRID = { radius: 5, hexSize: 1.0 };         // ← 与 3.6 m 一格等比

/** 把水面三角形投到湖区本地平面（存成引擎单位，frame 的 u/v 轴是米）。 */
export function waterTriangles(water) {
  const frame = water?.frame;
  if (frame?.kind !== 'local-tangent-plane') throw new Error('这是旧版曲面湖泊，请先在球系统把湖转成平面湖区');
  const o = frame.origin, u = frame.tangentU, v = frame.tangentV;
  const p = water.positions || [];
  const n = (p.length / 3) | 0;
  if (!n) throw new Error('这个湖没有水面三角形');
  const local = new Float64Array(n * 2);
  for (let i = 0; i < n; i++) {
    const dx = p[i * 3] - o[0], dy = p[i * 3 + 1] - o[1], dz = p[i * 3 + 2] - o[2];
    local[i * 2] = (dx * u[0] + dy * u[1] + dz * u[2]) * LAKE_UNITS_PER_METRE;
    local[i * 2 + 1] = (dx * v[0] + dy * v[1] + dz * v[2]) * LAKE_UNITS_PER_METRE;
  }
  const idx = water.indices;
  const tris = [];
  if (idx?.length) for (let k = 0; k < idx.length; k += 3) tris.push(idx[k], idx[k + 1], idx[k + 2]);
  else for (let k = 0; k < n; k += 3) tris.push(k, k + 1, k + 2);
  return { local, tris };
}

function insideTri(px, pz, ax, az, bx, bz, cx, cz) {
  const d1 = (px - bx) * (az - bz) - (ax - bx) * (pz - bz);
  const d2 = (px - cx) * (bz - cz) - (bx - cx) * (pz - cz);
  const d3 = (px - ax) * (cz - az) - (cx - ax) * (pz - az);
  const neg = d1 < 0 || d2 < 0 || d3 < 0, pos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(neg && pos);
}

/** 湖面覆盖到的格子（World 的 cx / cz 与 frame 的 u / v 是同一个米制坐标系）。 */
export function lakeWetCells(world, water) {
  const { local, tris } = waterTriangles(water);
  const wet = new Set();
  for (const cell of world.cells) {
    for (let k = 0; k < tris.length; k += 3) {
      const a = tris[k] * 2, b = tris[k + 1] * 2, c = tris[k + 2] * 2;
      if (insideTri(cell.cx, cell.cz, local[a], local[a + 1], local[b], local[b + 1], local[c], local[c + 1])) {
        wet.add(cell.id);
        break;
      }
    }
  }
  return wet;
}

/**
 * 水格保持 ground = 0（水面，可建造），湖岸按圈数抬高成岩石台阶。
 * 岩石层由地形管线出石头，建筑层由建筑管线出房子 —— 两条管线读同一份体素。
 */
export function paintLakeIntoWorld(world, water, { shoreRings = 3 } = {}) {
  const wet = lakeWetCells(world, water);
  if (!wet.size) throw new Error('湖区网格没有覆盖到这块水面，请检查湖的框架数据');
  const cells = world.cells;
  const shore = new Set();
  let ring = wet;
  for (let r = 0; r < shoreRings; r++) {
    const next = new Set();
    for (const id of ring) {
      const cell = cells[id];
      for (let i = 0; i < cell.n; i++) {
        const nb = cell.edgeNeighbors[i];
        if (nb < 0 || wet.has(nb) || shore.has(nb)) continue;
        next.add(nb);
      }
    }
    if (!next.size) break;
    const level = Math.min(MAX_SHORE_LEVEL, r + 1);
    for (const id of next) {
      world.fill(cells[id], level, { rock: true });
      shore.add(id);
    }
    ring = next;
  }
  world.touch();
  return { wet: wet.size, shore: shore.size, cells: cells.length, shoreRings };
}

/** 湖区本地坐标下的中心与跨度（引擎单位；乘 LAKE_METRES_PER_UNIT 得米）。 */
export function lakeCenterLocal(water) {
  const { local } = waterTriangles(water);
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < local.length; i += 2) {
    const x = local[i], z = local[i + 1];
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (z < minZ) minZ = z;
    if (z > maxZ) maxZ = z;
  }
  return {
    x: (minX + maxX) / 2, z: (minZ + maxZ) / 2,
    width: maxX - minX, depth: maxZ - minZ,
    widthMetres: (maxX - minX) * LAKE_METRES_PER_UNIT,
    depthMetres: (maxZ - minZ) * LAKE_METRES_PER_UNIT,
  };
}

/* ------------------------------------------------------------------ */
/* 存档：湖区只存体素（solid / rock / color），与 world 一一对应       */
/* ------------------------------------------------------------------ */

export const LAKE_STATE_VERSION = 1;

export function exportLakeState(world, meta = {}) {
  const cells = world.cells;
  const solid = new Array(cells.length), rock = new Array(cells.length), color = new Array(cells.length);
  for (let i = 0; i < cells.length; i++) {
    solid[i] = world.solid[i];
    rock[i] = world.rock[i];
    color[i] = world.color[i] + 1;      // -1（未上色）→ 0
  }
  return {
    v: LAKE_STATE_VERSION,
    grid: { radius: world.grid.radius, hexSize: world.grid.hexSize },
    lakeIndex: meta.lakeIndex ?? null,
    terrainVersion: meta.terrainVersion ?? null,
    cells: cells.length,
    solid, rock, color,
  };
}

export function importLakeState(world, state) {
  if (!state || state.v !== LAKE_STATE_VERSION) throw new Error('湖区存档版本不支持');
  const g = world.grid;
  if (state.grid && (state.grid.radius !== g.radius || Math.abs(state.grid.hexSize - g.hexSize) > 1e-6)) throw new Error('湖区存档的网格与当前网格不一致');
  const n = world.cells.length;
  if (state.cells !== n || state.solid?.length !== n) throw new Error('湖区存档的格子数与网格不一致');
  for (let i = 0; i < n; i++) {
    world.solid[i] = state.solid[i] | 0;
    world.rock[i] = state.rock[i] | 0;
    world.color[i] = (state.color[i] | 0) - 1;
  }
  world.touch();
  return { cells: n, terrainVersion: state.terrainVersion ?? null, lakeIndex: state.lakeIndex ?? null };
}

/** 楼栋统计：同高、顶上不是岩石的相邻格连成一栋（和 world.regions 同口径）。 */
export function lakeBuildingStats(world) {
  const seen = new Set();
  let buildings = 0, blocks = 0, floors = 0;
  for (const cell of world.cells) {
    const top = world.top(cell);
    if (!top || world.isRock(cell, top)) continue;
    blocks++;
    floors += top - world.ground(cell);
    if (seen.has(cell.id)) continue;
    buildings++;
    const stack = [cell];
    seen.add(cell.id);
    while (stack.length) {
      const c = stack.pop();
      for (let i = 0; i < c.n; i++) {
        const nb = c.edgeNeighbors[i];
        if (nb < 0 || seen.has(nb)) continue;
        const other = world.cells[nb];
        const ot = world.top(other);
        if (ot !== top || world.isRock(other, ot)) continue;
        seen.add(nb);
        stack.push(other);
      }
    }
  }
  return { buildings, blocks, floors };
}
