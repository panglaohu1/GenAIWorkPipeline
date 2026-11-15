# TigerMessenger 项目约束与入口

项目位置：`/Users/panglaohu/Downloads/TigerInBamboo/TigerMessenger`。2026-09-28 用户指定本顾问负责十二门徒岩群与高山圣城城堡方案；后续用户修正优先。

## 十二门徒

目标为偏海洋蓝灰色、有宽台面、高低错落和海蚀基座的海岸岩群；不能是尖锥或等宽圆柱加横线。轴向取各自脚下实际海面法线，基座入水，植被根部在最终承托面上。保留独立柱与有联系的台地，不机械把全部岩柱接成墙。铁路和通行区域为固定禁入边界。

已知旧实现 `src/world/seaStackWfc.js` 为纵向半径模块求解；接口匹配不等于视觉达标。用户明确否定缺台面、歪斜、悬空。检查 `gateSeaStacks.js`、`gateTargetThirty.js` 和真实调用，不只改模块库。

## 高山圣城城堡

保留已认可的新旧城间距和新城沿海露天铁路方向。保留港口上城通道、步行桥与既有场景身份；木马等地标须有承托。建筑需要历史厚重感，主体、翼楼、塔楼和台基有层级；不得照搬北欧白房子覆盖既有城堡。配色依据用户认可的伊斯坦布尔色彩研究与实际目标图，不能把单张夕阳照片直接烘焙成所有时间的紫橙材质。

方案先检查城墙是否堵路、台基是否贴山、建筑是否扎实；旧城/新城差异通过密度、尺度、材料风化表达，不是只换颜色。

## 复用入口

- `src/procgen/wfc/solver.js`：通用求解、传播、限界回溯，优先复用。
- `src/procgen/wfc/moduleSchema.js`：连接与承托/净空语义，几何边界仍需核验。
- `src/procgen/planet/terrainTiles.js`：语义目录，不等于已完成美术模块。
- `src/procgen/planet/vegetationCompilerV9.js`：生态、禁入、LOD。
- `src/render/clouds/impostorAtlasBuilder.js`、`cloudImpostorMaterial.js`：共享表示候选；并非已证实复制作者实现。

每次先读项目 AGENTS.md 与 docs/PROJECT_HANDOFF.md、PLAN.md、TODOS.md、artifacts/game-progress.md 的最新相关记录。几何更改按项目要求留下快照和同机位对照；此技能不替代现有 Three.js 专长和 Jev 流程。
