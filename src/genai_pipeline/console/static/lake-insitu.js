/**
 * lake-insitu.js — 球面上的湖区建造（球与场景一体）。
 *
 * 球面页不再跳去独立工坊：这里直接用 REACodeRefactoring 的 World 体素场 +
 * build/buildings.js 出墙 / 屋顶 / 门窗 / 支撑，再按湖面的 tangent frame 贴回球面。
 * 所以"在球上点一下"和"在工坊里点一下"改的是同一份体素、走同一条建筑管线。
 *
 * 本文件只做三件事：体素存档、球面坐标换算、烘出 BufferGeometry。
 * 不含任何自写房子几何。
 */
import * as THREE from 'three';
import { buildQuadGrid } from '/engines/REACodeRefactoring/src/core/grid.js';
import { World } from '/engines/REACodeRefactoring/src/core/world.js';
import { MeshWriter } from '/engines/REACodeRefactoring/src/core/meshwriter.js';
import { buildBuildings } from '/engines/REACodeRefactoring/src/build/buildings.js';
import { PALETTES } from '/engines/REACodeRefactoring/src/core/palette.js';
import {
  paintLakeIntoWorld, importLakeState, exportLakeState, lakeBuildingStats,
  LAKE_GRID, LAKE_METRES_PER_UNIT,
} from '/engines/system-workshop/planet/lake-scene.js';

export const MAX_LEVEL = 12;
export const LAKE_SCALE = LAKE_METRES_PER_UNIT;   // 1 引擎单位 = 7.2 m
const HALF_CELL = 0.25;                            // 格距 0.5 的一半（引擎单位）

/**
 * @param {object} opts
 * @param {object} opts.water    planet.inlandWater[i]（必须带 frame）
 * @param {object} [opts.scene]  已有的 lake-world 场景（读 worldState）
 * @param {string} [opts.palette]
 */
export function createLakeInsitu({ water, scene = null, palette = 'town' } = {}) {
  const grid = buildQuadGrid({ ...LAKE_GRID });
  const world = new World(grid);
  world.palette = PALETTES[palette] || PALETTES.town;

  // 先铺湖（水格 ground=0 可直接建造，湖岸抬成岩石），再用存档覆盖
  const painted = paintLakeIntoWorld(world, water, { shoreRings: 3 });
  let restored = null, restoreError = null;
  if (scene?.worldState) {
    try { restored = importLakeState(world, scene.worldState); }
    catch (err) { restoreError = String(err?.message || err); }
  }

  const frame = water.frame;
  const O = new THREE.Vector3(...frame.origin);
  const U = new THREE.Vector3(...frame.tangentU);
  const V = new THREE.Vector3(...frame.tangentV);
  const N = new THREE.Vector3(...frame.normal);

  /* ---- 坐标 ---------------------------------------------------------- */
  /** 球面点 → 湖区本地（引擎单位，y=0 是水面）。 */
  function toLocal(point) {
    const d = new THREE.Vector3(point.x - O.x, point.y - O.y, point.z - O.z);
    return { x: d.dot(U) / LAKE_SCALE, y: d.dot(N) / LAKE_SCALE, z: d.dot(V) / LAKE_SCALE };
  }

  /** 本地（引擎单位）→ 球面米制。 */
  function toSphere(x, y, z) {
    return [
      O.x + (U.x * x + V.x * z + N.x * y) * LAKE_SCALE,
      O.y + (U.y * x + V.y * z + N.y * y) * LAKE_SCALE,
      O.z + (U.z * x + V.z * z + N.z * y) * LAKE_SCALE,
    ];
  }

  /** 最近格心（cx / cz 与本地坐标同单位）。 */
  function nearestCell(x, z) {
    let best = null, bestD = Infinity;
    for (const cell of world.cells) {
      const dx = cell.cx - x, dz = cell.cz - z;
      const d = dx * dx + dz * dz;
      if (d < bestD) { bestD = d; best = cell; }
    }
    return { cell: best, dist: Math.sqrt(bestD) };
  }

  /** 球面点 → 命中格 + 层号。tolerance 默认半格多一点，点空返回 null。 */
  function pick(point, tolerance = HALF_CELL * 2.2) {
    if (!point) return null;
    const local = toLocal(point);
    const { cell, dist } = nearestCell(local.x, local.z);
    if (!cell || dist > tolerance) return null;
    const inside = dist <= HALF_CELL * 1.6;
    const level = Math.max(1, Math.min(MAX_LEVEL, Math.floor(local.y / 0.5) + 1));
    return {
      cell, level, dist, inside,
      local,
      top: world.top(cell),
      ground: world.ground(cell),
      wet: world.ground(cell) === 0,
      rock: world.top(cell) > 0 && world.isRock(cell, world.top(cell)),
    };
  }

  /* ---- 建造动作（与工坊 world/app.js 的 applyAt 同语义） ----------------- */
  function apply(cell, level, tool = 'build', colorIndex = 0) {
    if (!cell) return false;
    const top = world.top(cell);
    if (tool === 'erase') {
      const target = world.isSolid(cell, level) ? level : top;
      if (!target) return false;
      return world.setSolid(cell, target, false);
    }
    if (tool === 'paint') {
      if (world.color[cell.id] === colorIndex) return false;
      world.color[cell.id] = colorIndex;
      world.touch();
      return true;
    }
    const target = !world.isSolid(cell, level) ? level : Math.min(MAX_LEVEL, top + 1);
    if (target < 1 || world.isSolid(cell, target)) return false;
    world.setSolid(cell, target, true);
    if (target >= 2 || world.ground(cell) > 0) world.color[cell.id] = colorIndex;
    return true;
  }

  /**
   * 一次点击的完整决策：格由命中点的平面投影决定，动作由这一格当前高度决定。
   * 球面上只有三角面、没有体素身份，用命中高度反推层号会误判（打到远处屋顶），
   * 所以层号只认 world.top —— 和 Townscaper「点这格就往上长」一致。
   */
  function resolve(point, { erase = false, paint = false, colorIndex = 0, floors = 1, tolerance } = {}) {
    const hit = pick(point, tolerance);
    if (!hit) return null;
    const top = hit.top;
    if (erase) return { ...hit, tool: 'erase', level: Math.max(1, top) };
    if (paint) return { ...hit, tool: 'paint' };
    if (top === 0) return { ...hit, tool: 'build', level: Math.max(1, Math.min(floors, MAX_LEVEL)) };
    return { ...hit, tool: 'build', level: Math.min(MAX_LEVEL, top + 1) };
  }

  /* ---- 出图 ---------------------------------------------------------- */
  /** 用建筑管线把当前体素烘成球面几何。 */
  function buildGeometry() {
    const writer = new MeshWriter();
    buildBuildings(writer, world, { L: 0.5 });
    const n = writer.pos.length;
    const pos = new Float32Array(n), col = new Float32Array(n);
    for (let i = 0; i < n; i += 3) {
      const x = writer.pos[i], y = writer.pos[i + 1], z = writer.pos[i + 2];
      pos[i] = O.x + (U.x * x + V.x * z + N.x * y) * LAKE_SCALE;
      pos[i + 1] = O.y + (U.y * x + V.y * z + N.y * y) * LAKE_SCALE;
      pos[i + 2] = O.z + (U.z * x + V.z * z + N.z * y) * LAKE_SCALE;
      col[i] = writer.col[i] ?? 0.72;
      col[i + 1] = writer.col[i + 1] ?? 0.72;
      col[i + 2] = writer.col[i + 2] ?? 0.72;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geometry.computeVertexNormals();
    geometry.computeBoundingSphere();
    return geometry;
  }

  /* ---- 撤销 ---------------------------------------------------------- */
  const undoStack = [], redoStack = [];
  function pushUndo(snapshot) {
    undoStack.push(snapshot || world.snapshot());
    if (undoStack.length > 80) undoStack.shift();
    redoStack.length = 0;
  }
  function undo() {
    if (!undoStack.length) return false;
    redoStack.push(world.snapshot());
    world.restore(undoStack.pop());
    return true;
  }
  function redo() {
    if (!redoStack.length) return false;
    undoStack.push(world.snapshot());
    world.restore(redoStack.pop());
    return true;
  }

  /* ---- 供页面使用的薄封装 -------------------------------------------- */
  return {
    world, grid, frame: { O, U, V, N },
    painted, restored, restoreError,
    cells: () => world.cells,
    toLocal, toSphere, nearestCell, pick, resolve, apply, buildGeometry,
    pushUndo, undo, redo,
    canUndo: () => undoStack.length > 0,
    canRedo: () => redoStack.length > 0,
    stats: () => lakeBuildingStats(world),
    exportState: (meta = {}) => exportLakeState(world, meta),
    levelHeight: 0.5,
    cellMetres: LAKE_METRES_PER_UNIT * 0.5,
  };
}
