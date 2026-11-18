/**
 * upgrade-lake-scenes.mjs — 把旧版"按栋"湖区场景升级成湖区体素场（lake-world）。
 *
 * 旧记录存的是 buildings[{localPosition,floors,color}]，渲染用的是 storeyHouseModule
 * 那套简化的贴片房子（扁、比例不对）。这里把每栋写回 World 体素，再走
 * REACodeRefactoring 的建筑管线烘成网格：球面、编辑器、存档三处从此同一套几何。
 *
 * 坐标：旧数据是米制 3.6 m 方格；Wold 用"一格 0.5"的引擎单位，1 单位 = 7.2 m。
 * 网格是松弛过的不规则四边形，所以按最近格心吸附；一个格只能放一栋，撞了就往
 * 最近的水格让位。
 *
 * 用法：node scripts/upgrade-lake-scenes.mjs <planet.json> <lake-scenes.json>
 */
import { register } from 'node:module';
import { readFileSync, writeFileSync, copyFileSync } from 'node:fs';

register(new URL('../src/genai_pipeline/reacode/three-resolve.mjs', import.meta.url).href, import.meta.url);

const { buildQuadGrid } = await import('../engines/REACodeRefactoring/src/core/grid.js');
const { World, MAX_LEVEL } = await import('../engines/REACodeRefactoring/src/core/world.js');
const { PALETTES } = await import('../engines/REACodeRefactoring/src/core/palette.js');
const { MeshWriter } = await import('../engines/REACodeRefactoring/src/core/meshwriter.js');
const { buildBuildings } = await import('../engines/REACodeRefactoring/src/build/buildings.js');
const {
  paintLakeIntoWorld, lakeBuildingStats, lakeCenterLocal, exportLakeState, lakeWetCells,
  LAKE_GRID, LAKE_UNITS_PER_METRE, LAKE_METRES_PER_UNIT,
} = await import('../engines/system-workshop/planet/lake-scene.js');

const [planetPath, scenesPath] = process.argv.slice(2);
if (!planetPath || !scenesPath) throw new Error('用法：node scripts/upgrade-lake-scenes.mjs <planet.json> <lake-scenes.json>');

const planet = JSON.parse(readFileSync(planetPath, 'utf8'));
const scenes = JSON.parse(readFileSync(scenesPath, 'utf8'));
copyFileSync(scenesPath, scenesPath + '.pre-lake-world');

const upgraded = [];
for (const scene of scenes) {
  if (scene.kind === 'lake-world') { upgraded.push(scene); continue; }
  const water = planet.inlandWater?.[scene.lakeIndex];
  if (!water || water.frame?.kind !== 'local-tangent-plane') { upgraded.push(scene); continue; }

  const grid = buildQuadGrid({ ...LAKE_GRID });
  const world = new World(grid);
  world.palette = PALETTES.town;
  paintLakeIntoWorld(world, water, { shoreRings: 3 });
  const wet = lakeWetCells(world, water);
  const free = (cell) => wet.has(cell.id) && world.top(cell) === 0;
  // 旧数据是一栋一格、栋与栋之间留 3.6 m 的排屋。若把相邻格都填上，REA 会把它们
  // 合成一整片大屋顶，原来的排布就糊了。所以落位时要求四邻都是空的，保住小巷。
  const isolated = (cell) => free(cell) && cell.edgeNeighbors.every((id) => id < 0 || world.top(world.cells[id]) === 0);

  let placed = 0, shifted = 0, skipped = 0;
  for (const building of scene.buildings || []) {
    const p = building.localPosition || [0, 0, 0];
    const tx = p[0] * LAKE_UNITS_PER_METRE, tz = p[2] * LAKE_UNITS_PER_METRE;
    // 由近到远找第一个还空着的水格（旧数据可能两栋吸到同一格）
    const candidates = [...grid.cells].sort((a, b) => ((a.cx - tx) ** 2 + (a.cz - tz) ** 2) - ((b.cx - tx) ** 2 + (b.cz - tz) ** 2));
    const cell = candidates.find(isolated);
    if (!cell) { skipped++; continue; }
    if (Math.hypot(cell.cx - tx, cell.cz - tz) > 0.4) shifted++;
    // 旧模型 1 层 = 有墙有窗的一层；REA 体素第 1 层是石台基，所以整体 +1
    const floors = Math.max(2, Math.min(MAX_LEVEL, (building.floors || 1) + 1));
    for (let f = 1; f <= floors; f++) world.setSolid(cell, f, true);
    world.color[cell.id] = Number.isInteger(building.color) ? building.color % world.palette.colors.length : 0;
    placed++;
  }
  world.touch();

  const writer = new MeshWriter();
  buildBuildings(writer, world, { L: 0.5 });
  const stats = lakeBuildingStats(world);
  const local = lakeCenterLocal(water);
  const f = water.frame;
  const S = LAKE_METRES_PER_UNIT;
  // 球面 frame 是米制：体素网格是引擎单位，出图前整体乘回 7.2
  const positions = new Array(writer.pos.length);
  for (let i = 0; i < positions.length; i++) positions[i] = writer.pos[i] * S;
  const center = [0, 1, 2].map((i) => f.origin[i] + f.tangentU[i] * local.x * S + f.tangentV[i] * local.z * S);
  const len = Math.hypot(...center) || 1;

  upgraded.push({
    ...scene,
    kind: 'lake-world',
    center: center.map((v) => v / len),
    extent: { width: local.widthMetres, depth: local.depthMetres },
    frame: f,
    mesh: { positions, colors: writer.col },
    blocks: stats.blocks,
    floors: stats.floors,
    buildings: Array.from({ length: stats.buildings }, (_, i) => ({ id: `${scene.id}-b${i}` })),
    checks: {
      ...(scene.checks || {}),
      buildings: stats.buildings,
      blocks: stats.blocks,
      migratedFrom: scene.kind || 'lake-town',
      migratedBuildings: (scene.buildings || []).length,
      scope: 'lake voxels meshed by the REA building pipeline; gameplay and navigation not verified',
    },
    worldState: exportLakeState(world, { lakeIndex: scene.lakeIndex, terrainVersion: scene.terrainVersion }),
    capability: { id: 'planet.lake-world', source: 'engines/REACodeRefactoring/src/build/buildings.js', adapter: 'engines/system-workshop/planet/lake-scene.js' },
  });
  console.log(`${scene.id}: 旧 ${scene.buildings?.length || 0} 栋 → 落位 ${placed}（让位 ${shifted}、跳过 ${skipped}）→ 体素 ${stats.buildings} 栋 / ${stats.blocks} 块 / ${writer.pos.length / 3} 顶点`);
}

writeFileSync(scenesPath, JSON.stringify(upgraded, null, 2));
console.log('已写入', scenesPath);
