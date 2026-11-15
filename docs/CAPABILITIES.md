# 能力层（capabilities）

管线的能力现在和 TigerInBamboo 的能力库用同一套约定：**一个能力 = meta + match + run（+ verify）**。
本管线自己的能力用 Python 写、登记在 `src/genai_pipeline/capabilities/native.py`；
TigerInBamboo 的能力代码（JavaScript）不复制，运行时由 Node 子进程原样 import（`capabilities/bridge.mjs`）。

| 部件 | 文件 | 对应 TigerInBamboo |
|---|---|---|
| 能力契约、Registry、Router、经验、Watchdog、TierAgent | `capabilities/contract.py` | `registry.js`、`system-workshop/tier-agent.js`、`scene-authoring/agent/runtime.js` |
| 调外部能力的桥 | `capabilities/bridge.py` + `bridge.mjs` | 改造 Worker / 验证 Worker |
| 体素层（Oskar 角点状态 ↔ 体素 World） | `capabilities/voxel.py` | `REACodeRefactoring/src/capabilities`（registry + verify） |
| 物件层（组装布局 ↔ 物件布局） | `capabilities/objects.py` | `system-workshop/object`（capabilities + verify） |
| 通用场景层（材质色表 ↔ materials） | `capabilities/gscene.py` | `system-workshop/gscene`（capabilities + verify） |
| 本管线的能力 | `capabilities/native.py` | — |

## 能力一览

| 层 | 能力 | 来源 | 在管线里什么时候跑 |
|---|---|---|---|
| scene3d | `env.sky-dome` 实测天空盒 | 本管线 | 组装第一轮，按计划 |
| scene3d | `light.rig-fit` 布光闭环 | 本管线 | 同上；残差过大 verify fail → 撤回到日光预设 |
| scene3d | `camera.pitch-fit` 相机俯角闭环 | 本管线 | 第一轮、还没比过俯角时 |
| scene3d | `color.fix` 配色闭环 | 本管线 | 布光之后 |
| decompose | `split.audit` 拆分自检 | 本管线 | 检查型（只 match / verify） |
| object | `object.spacing-relax` 间距松弛 | 系统工坊 | 每轮布局定下来后（默认启用） |
| object | `object.scale-variety` 同类尺寸变化、`object.yaw-variety` 朝向变化 | 系统工坊 | 需求里提到「大小 / 变化 / 自然 / 朝向…」时 |
| gscene | `scene.style-pack` 套用风格包（莫里斯色板） | 系统工坊 | 需求里提到「莫里斯 / 挂毯 / 风格包…」时，配色闭环之后 |
| gscene | `scene.palette-unify` 主色归并、`scene.tone` 整体色调 | 系统工坊 | 需求里提到对应关键词时 |
| voxel | `terrain.arch` 石拱、`terrain.overhang` 崖顶悬垂、`terrain.seastack` 海中石柱、`terrain.tunnel` 穿山隧道、`build.lighthouse` 海边灯塔 | REACode 能力库 | Oskar 引擎建地形 / 场景程序时（每次 build_oskar），需求里提到对应关键词时 |
| voxel | `water.volume`、`build.lighthouse-light`、`effects.smoke` | REACode 能力库 | 显示层（three.js），本管线不执行，只在目录里列出 |
| planet | 海平面、重解地形、地表分辨率、切图数、网格细分 | 系统工坊 | 本管线只读描述 |

## 一次执行的流程

扫描（match：每个能力能作用到哪些物件、为什么）→ Router（需求描述里的关键词命中，或管线里已启用；
而且场景里确实有可作用的物件）→ 在**副本**上执行 → 独立验证（外部能力用它自己的 `verify.js`）→
不是 fail 才写回。通用场景层还要再过一道同机位渲染比对：平均色差多 1 以上或 DINO 掉 0.01 以上就不采用。

每一步记经验：`data/experience/<层>.jsonl`（预览 / 写入 / 放弃，按能力汇总）。
控制台 `GET /api/capabilities` 列出全部能力和各自的经验汇总。

Watchdog：外部能力每个阶段写一行心跳；20 秒无心跳或 60 秒总时长 → 杀掉子进程、重试一次（`config [capabilities] idle_s / timeout_s`）。

## 配置（config/pipeline.toml `[capabilities]`）

- `node`：Node 可执行文件（找不到时试 `/opt/homebrew/bin/node`、`/usr/local/bin/node`）。没有 Node 时外部能力跳过，管线照常跑。
- `reacode` / `workshop`：两个能力目录。
- `object_selected` / `gscene_selected` / `voxel_selected`：默认启用的能力（不看关键词也选）。

运行选项（每个任务的 options）：
`compose_plan`（本管线能力的执行顺序）、`capability_goal`（额外的改造方向文字）、`capability_params`（`{能力 id: {参数}}`）、
`object_capabilities` / `gscene_capabilities` / `voxel_capabilities`（false 关掉该层）、
`object_capabilities_selected` 等（覆盖默认启用）。

## 已知限制

- REACode 的「石拱」「穿山隧道」沿「对边直行」找路径，只在四边形格上成立；Oskar 默认的六边形网格上扫描不到可作用物件
  （quad 网格的程序可以用）。
- 物件层的单位：工坊能力参数按它的场景单位写，这里先按「占地半径中位数 = 0.5」换算，执行完换回米。

## 新增一个本管线的能力

在 `native.py` 写 `match / run / verify`（可选 `revert`：verify fail 时撤回），登记进 `NATIVE`；
要进组装计划就加进 `COMPOSE_PLAN` 或任务的 `compose_plan`。测试在 `tests/test_capabilities.py`。
