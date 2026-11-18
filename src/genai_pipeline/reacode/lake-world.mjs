/**
 * lake-world.mjs — 把湖区编辑器里的体素场烘成球面上的网格。
 *
 * 只用 REACodeRefactoring 自己的管线：World 体素 → build/buildings.js（墙 / 屋顶 / 门窗 / 支撑）。
 * 湖区编辑器（engines/REACodeRefactoring/index.html?run=…&lake=…）保存的就是这份体素，
 * 所以球面上看到的楼与编辑器里盖的完全一致，不存在第二套房子生成器。
 *
 * 输入 stdin: {input: planet.json 路径, out: 输出路径, options: {lakeIndex, terrainVersion, sceneId, state}}
 */
import { register } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';

register(new URL('./three-resolve.mjs', import.meta.url).href, import.meta.url);

const payload = JSON.parse(await new Promise((resolve, reject) => {
  const chunks = [];
  process.stdin.on('data', (c) => chunks.push(c));
  process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
  process.stdin.on('error', reject);
}));

const { buildQuadGrid } = await import('../../../engines/REACodeRefactoring/src/core/grid.js');
const { World, MAX_LEVEL } = await import('../../../engines/REACodeRefactoring/src/core/world.js');
const { PALETTES } = await import('../../../engines/REACodeRefactoring/src/core/palette.js');
const { MeshWriter } = await import('../../../engines/REACodeRefactoring/src/core/meshwriter.js');
const { buildBuildings } = await import('../../../engines/REACodeRefactoring/src/build/buildings.js');
const { importLakeState, lakeBuildingStats, lakeCenterLocal, LAKE_GRID, LAKE_METRES_PER_UNIT } = await import('../../../engines/system-workshop/planet/lake-scene.js');

const planet = JSON.parse(readFileSync(payload.input, 'utf8'));
const options = payload.options || {};
const lakeIndex = options.lakeIndex;
const state = options.state;
const water = planet.inlandWater?.[lakeIndex];
if (!water || water.kind !== 'lake') throw new Error('这个编号不是已经生成水面的湖泊');
if (water.frame?.kind !== 'local-tangent-plane') throw new Error('湖泊还不是平面湖区，请先在球系统转换');

// 与前端 GRIDS.lake 必须完全同参，否则体素索引对不上。
// 建筑管线按 0.5 单位一格设计（层高 0.5、窗高 .095 都是绝对数），
// 所以这里也用 0.5，出网格后整体乘 LAKE_METRES_PER_UNIT 换回 3.6 m 一格的米制。
const grid = buildQuadGrid({ ...LAKE_GRID });
const world = new World(grid);
world.palette = PALETTES.town;
importLakeState(world, state);

for (let i = 0; i < world.solid.length; i++) {
  if ((world.solid[i] & ~((1 << MAX_LEVEL) - 1)) || world.rock[i] & ~world.solid[i]) throw new Error('湖区体素越界');
}

const writer = new MeshWriter();
const result = buildBuildings(writer, world, { L: 0.5 });
const stats = lakeBuildingStats(world);
if (!writer.pos.every(Number.isFinite)) throw new Error('湖区网格出现非有限坐标');

// 引擎单位 → 米：球面的 frame（origin / tangentU / tangentV）是米制，不换算会把楼缩成 1/7.2。
const S = LAKE_METRES_PER_UNIT;
const positions = new Array(writer.pos.length);
for (let i = 0; i < positions.length; i++) positions[i] = writer.pos[i] * S;

// 球面上的场景中心（方向向量）：球系统按它做区域归属与相机聚焦。
const local = lakeCenterLocal(water);
const f = water.frame;
const center = [0, 1, 2].map((i) => f.origin[i] + f.tangentU[i] * local.x * S + f.tangentV[i] * local.z * S);
const centerLength = Math.hypot(...center) || 1;
const centerDir = center.map((v) => v / centerLength);

const scene = {
  id: options.sceneId,
  kind: 'lake-world',
  lakeIndex,
  terrainVersion: options.terrainVersion,
  frame: water.frame,
  center: centerDir,
  extent: { width: local.widthMetres, depth: local.depthMetres },
  mesh: { positions, colors: writer.col },
  buildings: Array.from({ length: stats.buildings }, (_, i) => ({ id: `${options.sceneId}-b${i}` })),
  blocks: stats.blocks,
  floors: stats.floors,
  posts: result.posts?.length ?? 0,
  checks: {
    finite: true,
    cells: world.cells.length,
    wet: world.cells.filter((c) => world.top(c) === 0).length,
    buildings: stats.buildings,
    blocks: stats.blocks,
    scope: 'lake voxels meshed by the REA building pipeline; gameplay and navigation not verified',
  },
  capability: {
    id: 'planet.lake-world',
    source: 'engines/REACodeRefactoring/src/build/buildings.js',
    adapter: 'engines/system-workshop/planet/lake-scene.js',
    editor: 'engines/REACodeRefactoring/index.html（Townscaper 建造工坊）',
  },
  worldState: state,
};

writeFileSync(payload.out, JSON.stringify(scene));
