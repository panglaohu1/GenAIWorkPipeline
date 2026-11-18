/**
 * upgrade-lake-scenes.mjs — 把旧版"按栋"湖区场景升级成湖区体素场（lake-world）。
 *
 * 旧记录存的是 buildings[{localPosition,floors,color}]，渲染用的是 storeyHouseModule 的另一套房子。
 * 这里把每栋写回 World 体素，再走 REACodeRefactoring 的建筑管线烘成网格，
 * 于是球面、编辑器、存档三处用的是同一套几何。
 *
 * 用法：node scripts/upgrade-lake-scenes.mjs <planet.json> <lake-scenes.json>
 */
import { register } from 'node:module';
import { readFileSync, writeFileSync, copyFileSync } from 'node:fs';

register(new URL('../src/genai_pipeline/reacode/three-resolve.mjs', import.meta.url).href, import.meta.url);

const { buildQuadGrid } = await import('../engines/REACodeRefactoring/src/core/grid.js');
const { World } = await import('../engines/REACodeRefactoring/src/core/world.js');
const { PALETTES } = await import('../engines/REACodeRefactoring/src/core/palette.js');
const { MeshWriter } = await import('../engines/REACodeRefactoring/src/core/meshwriter.js');
const { buildBuildings } = await import('../engines/REACodeRefactoring/src/build/buildings.js');
const { paintLakeIntoWorld, lakeBuildingStats, lakeCenterLocal, exportLakeState, lakeWetCells } = await import('../engines/system-workshop/planet/lake-scene.js');

const [planetPath, scenesPath] = process.argv.slice(2);
if (!planetPath || !scenesPath) throw new Error('用法：node scripts/upgrade-lake-scenes.mjs <planet.json> <lake-scenes.json>');

const planet = JSON.parse(readFileSync(planetPath, 'utf8'));
const scenes = JSON.parse(readFileSync(scenesPath, 'utf8'));
copyFileSync(scenesPath, scenesPath + '.pre-lake-world');

const nearestCell = (grid, x, z) => {
  let best = grid.cells[0], bestD = Infinity;
  for (const cell of grid.cells) {
    const d = (cell.cx - x) ** 2 + (cell.cz - z) ** 2;
    if (d < bestD) { bestD = d; best = cell; }
  }
  return best;
};

const upgraded = [];
for (const scene of scenes) {
  if (scene.kind === 'lake-world') { upgraded.push(scene); continue; }
  const water = planet.inlandWater?.[scene.lakeIndex];
  if (!water || water.frame?.kind !== 'local-tangent-plane') { upgraded.push(scene); continue; }

  const grid = buildQuadGrid({ radius: 5, hexSize: 7.2 });
  const world = new World(grid);
  world.palette = PALETTES.town;
  paintLakeIntoWorld(world, water, { shoreRings: 3 });
  const wet = lakeWetCells(world, water);

  let placed = 0;
  for (const building of scene.buildings || []) {
    const p = building.localPosition || [0, 0, 0];
    const cell = nearestCell(grid, p[0], p[2]);
    if (!wet.has(cell.id)) continue;                 // 旧数据里落不到水面的，跳过
    // 旧模型的 1 层 = 台基 + 一层墙 + 屋顶；REA 体素里第 1 层是石台基，
    // 所以墙要从第 2 层起算，层数整体 +1 才是同一栋房子。
    const floors = Math.max(2, Math.min(12, (building.floors || 1) + 1));
    for (let f = 1; f <= floors; f++) world.setSolid(cell, f, true);
    world.color[cell.id] = Number.isInteger(building.color) ? building.color : 0;
    placed++;
  }
  world.touch();

  const writer = new MeshWriter();
  buildBuildings(writer, world, { L: 0.5 });
  const stats = lakeBuildingStats(world);
  const local = lakeCenterLocal(water);
  const f = water.frame;
  const center = [0, 1, 2].map((i) => f.origin[i] + f.tangentU[i] * local.x + f.tangentV[i] * local.z);
  const len = Math.hypot(...center) || 1;

  upgraded.push({
    ...scene,
    kind: 'lake-world',
    center: center.map((v) => v / len),
    extent: { width: local.width, depth: local.depth },
    frame: f,
    mesh: { positions: writer.pos, colors: writer.col },
    blocks: stats.blocks,
    floors: stats.floors,
    buildings: scene.buildings || [],
    checks: {
      ...(scene.checks || {}),
      buildings: stats.buildings,
      blocks: stats.blocks,
      migratedFrom: scene.kind || 'lake-town',
      scope: 'lake voxels meshed by the REA building pipeline; gameplay and navigation not verified',
    },
    worldState: exportLakeState(world, { lakeIndex: scene.lakeIndex, terrainVersion: scene.terrainVersion }),
    capability: { id: 'planet.lake-world', source: 'engines/REACodeRefactoring/src/build/buildings.js', adapter: 'engines/system-workshop/planet/lake-scene.js' },
  });
  console.log(`${scene.id}: ${scene.buildings?.length || 0} 栋 → 落到水面 ${placed} 栋，烘出 ${stats.buildings} 栋 / ${writer.pos.length / 3} 顶点`);
}

writeFileSync(scenesPath, JSON.stringify(upgraded, null, 2));
console.log('已写入', scenesPath);
