# 研究知识与覆盖范围

更新：2026-09-28。X 媒体时间线已按半年窗口检索完毕（528/545 条原帖文字已读，7 条 GIF 抽帧，见 x-timeline.md）；演讲仍未观看，GIF 画面大部分未看。用户已登录 Codex 内置浏览器；标签列表可见，DOM/evaluate 两次超时，不能归因为用户未授权。旧公开视频取样为9段、36个取样时刻，其中一段后3帧为暗画面；不是36个有效完整片段。

## 已读来源

- WFC原作者仓库 https://github.com/mxgmn/WaveFunctionCollapse ：约束传播和选择；项目介绍指出 Bad North 有维持导航的选择启发式，Townscaper 结合 WFC、Marching Cubes 与非规则网格。这是算法作者对应用的说明，不是两款游戏的完整实现源码。
- Oskar 双网格帖（2021） https://x.com/OskSta/status/1448248658865049605 ，作者文字镜像 https://threadreaderapp.com/scrolly/1448248658865049605 ：区分玩法网格与表现网格。不是所有项目采用同一拓扑的证据。
- 草片实验（2022） https://x.com/OskSta/status/1590669875869286400 ，已读作者镜像 https://threadreaderapp.com/thread/1590669875869286400.html ：背景对比与深度处理；属于表现技术，不是 WFC 负责的植被生长。
- 混合网格及特殊岩形（2023） https://x.com/OskSta/status/1670790425232175108 ，已读作者镜像 https://threadreaderapp.com/thread/1670790425232175108.html ：模块上下文限制及网格比较；动态样本见项目媒体索引。后期实验不能倒推已发行游戏实现。

## 定位但未完整观看

- Bad North WFC 演讲：https://www.youtube.com/watch?v=0bcZb-SsnrA
- Bad North 视觉演讲：https://www.youtube.com/watch?v=6JcFbivo8dQ
- Townscaper/Organic Towns：https://www.youtube.com/watch?v=1hqt8JkYRdI
- Beyond Townscapers：https://www.youtube.com/watch?v=Uxeo9c-PX-w
- 树 impostor 候选：https://x.com/OskSta/status/1849427564034498642
- 云候选：https://x.com/OskSta/status/1852421920219635966

## 更新规范

每条新增结论保留原链接、作者/年代、作品、已读区间或取样时间、文字陈述、画面观察、工程推断及限制。镜像明确标镜像；动态观察不等于算法证明。冲突按日期和作品分开，而非简单覆盖。研究未完成的事项进入待验证列表，不能升级为权威技能事实。

## 我们的工程推断

宏观布局/固定边界 → 地形或建筑模块 → 约束求解 → 几何拼接 → 统一表面采样 → 植被、云、水 → 碰撞导航与视觉检查。全局通行另查，云运动另算；这条整合流程是 TigerMessenger 的提案，不能称 Oskar 私有引擎架构。
