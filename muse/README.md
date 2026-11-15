# Muse ⇄ GenAI Pipeline 文件桥

Muse（Meta 的 Mac 个人智能体）不支持 MCP，但能读写本机文件。本目录就是它与流水线之间的“收发箱”：

- **提交任务**：在 `muse/inbox/` 新建一个 `.md` 或 `.json` 文件，约 10 秒内控制台会自动建立运行，文件移到 `inbox/processed/`。
- **查看进展**：`muse/outbox/STATUS.md`（所有运行一览），`muse/outbox/<运行ID>.md`（阶段、每个资产最佳轮次与评分、对比图、GLB 与交付包路径）。每 10 秒刷新。

## Markdown 任务格式

```markdown
# 侦察飞机 v2
模式: asset
项目: scout-aircraft
低多边形玩具感的单座侦察机，浅蓝色机身……（需求描述写在这里）
```

模式可选 `game`（完整游戏）/ `asset`（单个资产）/ `scene`（复杂场景拆分组装）。
指定项目且没有附参考图时，自动使用 `projects/<项目>/inputs/` 中的目标图。

## JSON 任务格式

```json
{"title": "高山圣城 · 新城", "mode": "scene", "project": "holy-city-old-town",
 "brief": "……", "references": ["/Users/panglaohu/Pictures/new-city-target.png"],
 "options": {"model_max_rounds": 10, "plateau_patience": 5}}
```

## 给 Muse 的使用说明（可以直接对 Muse 说）

> 当我让你“用流水线做一个资产/场景”时：在 /Users/panglaohu/Downloads/GenAIWorkPipeline/muse/inbox/ 新建一个 Markdown 文件，
> 第一行写“# 标题”，然后写“模式: asset/scene/game”和“项目: 项目id”，再写需求。
> 之后读取 /Users/panglaohu/Downloads/GenAIWorkPipeline/muse/outbox/STATUS.md 和对应的 <运行ID>.md 向我汇报进度和评分。

## 连接方式与现状（2026-09-26 更正）

- 之前写的“Muse 设置 → 文件系统访问 → 允许访问指定文件夹”是**未经核实的推测**：Muse 实际的“文件系统访问”设置管的是 Mail、微信等应用数据的读写权限和一个屏蔽文件夹列表，没有“允许访问指定文件夹”的开关。已删除该步骤。
- Muse 反馈当前与本机的 native 文件连接尚未建立，收发箱协议暂时无法直接使用。
- 过渡方案：代码推送到 GitHub 仓库，Muse 从 GitHub 读取代码、`muse/` 目录与使用手册。本机文件连接打通后再启用收发箱。
- 收发箱本身已实现并测试通过（控制台运行时每 10 秒处理 `muse/inbox/`、刷新 `muse/outbox/`）。
