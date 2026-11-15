"""Oskar 顾问 Agent × Bonsai 提示词套件。

设计依据（本项目实测的 Bonsai 短板 → 补救）：
1. 空间坐标弱（蓝图时代：悬空、比例错）→ 只写少量「原语」、归一化平面坐标、高度用层数；
   悬空自动补台基、塔楼与拱门由引擎规则涌现（Oskar：玩家掌控大形，系统做小形）。
2. 整图理解与定位弱 → 读图拆成固定问卷（数量 / 左右顺序 / 高宽比 / 层次），并附 SAM 3 实测实例框作为客观数据。
3. 思考耗尽预算、输不出 JSON → 短输出、严格字段、少样本示例、思考预算上限、带错误信息重试。
4. 自评分虚高 → 评审只列差异、不打分；好坏由客观相似度 + 硬检查判定。
5. 补丁有效率低（16%）→ 受限编辑操作，每轮 ≤3 处小改，客观分不升即回滚。
6. 忘记约束 → 约束写进引擎（承托、通路、城垛、塔楼），提示词附自查清单。
"""

SYSTEM = """你是「Oskar 方法场景导演」，与一个确定性建模引擎合作（方法来自 Bad North / Townscaper 的公开研究，你不是 Oskar 本人）。
分工：你只决定**大形**——有什么、在哪、多大、多高、谁主谁次；引擎自动完成网格、墙面模块、窗门、城垛、屋顶、岩层、植被、描边与配色。
不要描述细节造型，不要写代码，不要写坐标以外的几何。

坐标约定（必须遵守）：
- 平面坐标 x, y ∈ [-1, 1]，(0,0) 为场景中心；x 向右为正；y 向画面深处为正（y 为负 = 离镜头近、在画面下方）。
- 尺寸 r（半径）、size [宽, 深] 也是归一化值：0.1 ≈ 场景半宽的十分之一。
- 高度一律用「层」（整数），1 层 ≈ 2 米。sea_level 表示海面在第几层顶面。
- 基础格是三角网的对偶格——六边形（零星五边形 / 七边形），不是四边形棋盘；你写的形状会落到六边形格上。

引擎会自动做（你不必也不要重复做）：
- terrain：自然地形由瓦片 WFC 坍缩（草坡 / 林地 / 裸岩崖 / 海 × 高度层，没有噪声）：从山峰向外广度优先、越往外越低，
  相邻格高差 ≤ 1 层，只有裸岩崖允许陡落；不留坑；林地与裸岩成片；plateaus 先给大模块（宫城、村落、广场）留出平整台地；
- 建筑下方悬空会自动补石台基；城墙转折点与端点自动长出塔楼；城墙门洞自动做成拱门；
- 城墙顶自动加城垛与走道；实心与空格的交界自动成墙，墙面由 WFC 选窗、门、拱窗、箭孔；
- 岩柱自动带竖向棱脊、通高裂隙、水线海蚀凹槽、泡沫与崩落石；台面自动成片长草与灌木（vegetation "trees" 时自动成林）；
- 岩体自动收分：低于周围平滑顶面的岩体向外放坡（高处收、低处放），崖面成坡、高岩柱上窄下宽，不会“蘑菇化”；
  海蚀岩柱场景还会把平面轮廓软化成圆润的柱体（程序可写 "batter": 0–0.8、"soften": 0–0.8 调整，0 为关闭）；
- 草地台缘自动长出下垂的草唇边和一圈灌丛带，台面是三阶明度的草地团块，水线外自动画 1–2 圈平行波纹；
- 屋顶颜色在 roof / roof_b / roof_c 色域里按建筑取样（主楼用主色）；
- palette "auto" 时引擎从目标图按语义区域（岩、水、建筑、屋顶、树、晶体）自动取色；
- 调色盘 citadel = 地中海宫城风格：窗全部为拱窗，主楼 / 翼楼顶层自动成连续拱廊，平顶自动加栏杆，
  城墙塔楼为方形城垛塔，台地外沿自动种成排的高柏树；主楼写 roof "dome" 时自动做「平顶 + 中央鼓座（拱窗）+ 穹顶 + 采光亭」。"""

READ = """读目标图，只回答下面的问卷，输出 JSON。看不清就写 null，不要猜测细节。

{facts}

问卷：
1. scene_type: "sea_stacks"（海蚀岩柱群）| "citadel"（崖上城堡 / 宫城）| "town" | "fantasy"（奇幻：晶体塔、岩岛、湖盆等）| "other"
2. main_subjects: 画面主体按从左到右列出，每项 {{"name": 中文名, "x": 画面水平位置 0(左)..1(右), "depth": "近"|"中"|"远",
   "width": 相对画面宽度 0..1, "height_to_width": 高÷宽（岩柱常见 1.5–3，城堡塔楼 3–5，主楼 0.6–1.2）,
   "tiers": 从下到上可分几级台阶/体块, "notes": 一句话}}
3. dominant: 哪一项最高最大（视觉焦点）
4. has_sea: 是否有海/水面；waterline: 主体底部是否直接入水
5. buildings（有建筑时填，否则 null）: {{"curtain_wall": 是否有城墙, "towers": 塔楼数量, "tower_roofs": 塔顶主要是 "dome"|"cone"|"crenel"（方形城垛）,
   "domes": 穹顶数量, "main_dome": 主楼中央是否有大穹顶, "roofs": 屋顶主要是 "flat"（平顶/露台）| "hip"（坡屋顶）,
   "gate": 是否有门洞, "plinth_levels": 台基/台地层数, "on_cliff": 是否建在崖上,
   "waterfalls": [{{"x": 图像水平位置 0..1, "top": 瀑布顶端在画面的高度 0(上)..1(下)}}]（没有就写 []）}}
6. bridges: 画面里的桥 / 高架桥 / 铁路桥 / 轨道（没有就写 []），每座 {{"from_x": 0..1, "to_x": 0..1, "depth": "近"|"中"|"远",
   "type": "arch"（石砌拱桥、连拱高架）| "rail"（细长轨道、廊桥、单轨）, "arches": 可见拱洞数, "notes": 从哪里连到哪里}}
7. crystals: 透明晶柱塔 / 晶簇（没有就写 []），每个 {{"x": 0..1, "depth": "近"|"中"|"远", "width": 0..1, "height_to_width": 高÷宽}}
8. big_trees: 显眼的大树（没有就写 []），每个 {{"x": 0..1, "depth": ..., "size": "giant"|"normal"}}；
   vegetation: 植被主要是 "shrub"（灌丛）| "trees"（成片大冠树）| "cypress"（成排柏树）
9. inner_water: 岛 / 台地内部是否有湖或池塘（没有就写 null），{{"x": 0..1, "width": 0..1, "depth": ...}}；
   paths: 可见的石板路 / 台阶 / 步道（没有就写 []），每条 {{"from_x": 0..1, "to_x": 0..1, "notes": 从哪到哪，是否有台阶}}
10. palette_hint: "sea_stack"（冷灰岩 + 青绿海）| "citadel"（暖象牙石 + 金穹顶）| "auto"（都不像：引擎从目标图自动取色）

只输出 JSON 对象，例如 {{"scene_type": "...", "main_subjects": [...], ...}}。"""

PLAN = """根据读图结果写场景程序。

读图结果：
{read}

{facts}

程序格式（只能用这些原语）：
{{"scene": 名称, "palette": "sea_stack"|"citadel"|"auto", "vegetation": "shrub"|"trees"|"cypress", "seed": 整数,
  "grid": {{"radius": 8, "cell": 2.0}}, "level_h": 2.0, "sea_level": 1 或 null,
  "items": [
   {{"type": "terrain", "at": [x, y], "r": 岛半径 0.5–0.9, "top": 最高层, "peaks": [{{"at": [x, y], "top": 层, "r": 山体半径}}],
     "plateaus": [{{"at": [x, y], "r": 台地半径, "top": 层}}], "forest": 0.1–0.5, "cliffs": 0.02–0.15, "wobble": 0.1–0.35}},
     自然地形（岛、山、林、海岸）：必须是 items 第一个；peaks 决定山在哪、多高；plateaus 给要放建筑的地方留平台；
     forest 越大林地越多，cliffs 越大裸岩崖越多，wobble 越大海岸线越曲折
   {{"type": "stack", "at": [x, y], "tiers": [{{"r": 半径, "top": 顶层, "shift": [dx, dy]}}, ...]}},   岩柱：tiers 从宽到窄、从低到高
   {{"type": "mass", "kind": "plinth"|"rock", "at": [x, y], "size": [宽, 深], "rot": 度, "bottom": 层, "top": 层, "roof": "flat"|"garden"}},
   {{"type": "building", "kind": "main"|"wing"|"tower"|"house", "at": [x, y], "size": [宽, 深] 或 "r": 半径,
     "bottom": 层, "top": 层, "roof": "hip"|"dome"|"cone"|"flat"}},   main + "dome" = 平顶 + 中央鼓座穹顶；tower + "dome" = 塔顶穹顶
   {{"type": "wall", "path": [[x, y], ...], "bottom": 层, "top": 层, "tower_roof": "crenel"|"dome"|"cone",
     "gates": [{{"at": [x, y], "height": 2}}]}},
   {{"type": "path", "points": [[x, y], ...], "levels": [起点层, 终点层], "clear": 2}},   通路：高度不同时引擎自动做台阶和堤道
   {{"type": "waterfall", "at": [x, y], "dir": [0, -1], "width": 0.08–0.14}},   瀑布：从 at 所在台地顺 dir 方向逐级跌落入海
   {{"type": "bridge", "kind": "arch"|"rail", "points": [[x, y], ...], "level": 桥面层, "width": 0.05}},   arch：石拱桥 / 连拱高架桥（桥墩与半圆拱由引擎生成）；rail：细长轨道 / 廊桥（细梁细柱）
   {{"type": "crystal", "at": [x, y], "r": 0.04–0.12, "top": 顶端层}},   晶柱簇（主晶柱 + 卫星晶柱 + 金铜平台），立在所在位置的地面上
   {{"type": "tree", "at": [x, y], "r": 树冠半径 0.03–0.15, "height": 树高层数}}   单棵大冠树（巨树 r 0.1–0.15）
  ]}}

从读图换算到程序（按公式算，不要凭感觉压缩位置）：
- 平面 x = (图像 x − 0.5) × 1.8，限制在 −0.9..0.9；主体要像目标一样横向铺开，不要挤在中间。
- 深度 → 平面 y：近 = −0.45..−0.1；中 = −0.05..0.3；远 = 0.35..0.8。同一深度的多个主体 y 可错开 ±0.08。
- 半径 r ≈ 图像宽度 × 0.9（远处的主体在图里显小，r 取 0.05–0.08 即可）；任何 r 不小于 0.05。
- 层数：top ≈ sea_level + round(高÷宽 × r × 27)（1 层 2 米，r=1 对应 27 米），主体最高，远处的更矮。

用 terrain 还是 stack：
- 自然岛屿、山丘、森林、海湾、小星球地块 → 第一个 item 写 terrain；山峰写进 peaks（主峰最高），
  建筑群所在处写 plateaus（top = 建筑的 bottom），建筑、路、桥、瀑布写在 terrain 后面。
- 海蚀岩柱群、竖直的孤立岩柱 → 用 stack（terrain 做不出高瘦岩柱）。两者可以同时用：terrain 做主岛，stack 做海里的岩柱。

硬规则（引擎会检查）：
- 所有东西必须连到地面（第 0 层）；建筑的 bottom 应等于它脚下台基/岩体的 top。
- 岩柱：大岩柱（r ≥ 0.12）至少两级宽台面（相邻 tiers 的 r 相差 25% 以上），远处小岩柱一两级即可；主岩柱高÷宽 ≥ 2（top 层数 × 2 米 ÷ (2 × r × 27 米) ≥ 2）；
  高低错落：主岩柱最高，其余依次递减，远处的更小更矮。
- 崖体、山岩用 stack（rock）；台基 plinth 只写 1–2 层，放在岩崖顶上（bottom = 岩崖 top）。
- 山坡城镇（如海港新城）：用 3–4 个 mass rock 从海边向深处逐级抬高（每级 2 层，越往后越窄），教堂 / 主楼占最高一级，
  民居 house（roof "hip"）分排放在各级台地上（bottom = 该级 top），一条 path 从码头直上最高台；码头连拱用 bridge arch 放在最前面的水上。
- 崖上宫城（旧城）：先用 stack 写出 2–3 级宽台地的崖体（台宽要真实，引擎会自动收分成坡），宫城放在最高台，瀑布从中层台地跌落。
- 城堡：主楼（main）最高最大 > 塔楼（tower，窄而高，r 0.05–0.09）> 翼楼（wing）> 城墙（wall）> 台基（plinth）；
  城墙沿台基**前缘**（靠镜头一侧），门洞放在城墙上；path 从门外（可以是海边低处）经门洞通向内部，levels=[起点层, 城墙 bottom]。
- 屋顶照读图：读图 roofs="flat" → 主楼 / 翼楼用 "flat"（有主穹顶时主楼用 "dome"）；tower_roofs 决定塔楼 roof 与城墙 tower_roof；
  穹顶数量 = 主楼 "dome"（1 个）+ 塔楼 "dome" 的个数。
- 桥：读图 bridges 里的每座桥写一个 bridge（**不要用 wall 代替桥**，wall 只用于城堡城墙；海蚀岩柱场景一般没有 wall）；
  points 按 from_x → to_x 换算，近景桥 y 取 −0.9..−0.6 并放在岩柱**前方且不要穿过岩柱**；level = sea_level + 2..4。
- 配色：palette_hint 为 auto 时 palette 写 "auto"；vegetation 照读图（成片大树 → "trees"）。
- 晶柱塔：读图 crystals 每个写一个 crystal（**不要用 building/tower 代替**）；top 按高宽比换算，主晶柱最高；
  晶柱应立在岩岛 / 台地上（at 落在某个 stack 或 mass 的范围内）。大树：读图 big_trees 每棵写一个 tree，巨树 r 取 0.1–0.15。
- 湖盆 / 环形崖 / 岛内池塘（读图 inner_water）：先写岛（stack 或 mass），再用 {{"type": "clear", "at": [x, y], "r": 半径, "bottom": sea_level}}
  挖出湖面（露出海面即成湖）；池塘旁的建筑、树不要放在挖空范围里。
- 路与台阶（读图 paths）：每条写一个 path，points 沿路走向，levels=[起点层, 终点层]（台阶由引擎自动做成坡道）；
  房子门前、桥头、码头之间都应有 path 连通。
- 瀑布：读图有瀑布才写；at 放在城墙 / 建筑**前方**的岩台上（不要放在建筑里），dir 指向海（通常 [0, -1] 朝镜头）；
  x 按读图位置换算，width 0.08–0.14（瀑布是画面重点时取大）。
- 尺寸下限：building/stack 的 r ≥ 0.05、size 每边 ≥ 0.1，否则覆盖不到任何格子。
- items 总数 3–14 个。

自查清单（输出前在心里过一遍，不要写出来）：主体数量与读图一致？左右顺序一致？谁最高一致？没有悬空？门洞有 path？
穹顶个数与屋顶类型与读图一致？瀑布、桥、晶柱、大树、岛内池塘、路有没有漏写？有没有把桥写成 wall、把晶柱写成 tower？

参考示例（结构参考，不要照抄数值）：
{examples}

只输出程序 JSON 对象（标准 JSON：不要注释、不要尾逗号、不要解释文字）。"""

REVIEW = """左图是目标，右侧是当前引擎渲染（同一构图的若干机位）。{metrics}
引擎的细节（窗、城垛、岩层等）不用评价，只评价**大形**：数量、位置、左右顺序、高低主次、高宽比、台阶层次、是否入水、城墙走向。

当前程序：
{program}

列出最多 3 条最影响「像不像」的大形差异，每条差异直接带上一个编辑操作的字段。优先顺序：
1）硬检查未过的项（若有）；2）构图：主体分布是否像目标一样铺开、左右顺序与前后层次；3）主次高低与高宽比；4）台阶层次。可用操作：
- move：{{"what": "差异", "op": "move", "item": 序号, "dx": ±0.02–0.15, "dy": ±0.02–0.15}}
- scale：{{"what": "差异", "op": "scale", "item": 序号, "factor": 0.7–1.4}}（放大/缩小 r 或 size）
- top：{{"what": "差异", "op": "top", "item": 序号, "tier": 序号或 -1, "delta": ±1–4}}（调高/调低层数；tier 仅 stack 用）
- add_tier：{{"what": "差异", "op": "add_tier", "item": 序号, "r": 半径, "top": 层}}（给岩柱加一级）
- remove：{{"what": "差异", "op": "remove", "item": 序号}}
- add：{{"what": "差异", "op": "add", "item_json": {{完整原语}}}}
注意括号配对：每条差异是一个扁平对象，只有 add 的 item_json 内部有嵌套。
只输出 JSON：{{"diffs": [ {{...}}, {{...}} ]}}"""


def examples_text(kind: str | None = None) -> str:
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "projects" / "oskar-trials" / "programs"
    out = []
    for name in ("hex-island.ref.json", "sea-stacks.ref.json", "citadel.ref.json"):
        p = root / name
        if p.exists():
            out.append(json.dumps(json.loads(p.read_text()), ensure_ascii=False, separators=(",", ":")))
    return "\n".join(out)


def apply_edits(program: dict, edits: list[dict]) -> tuple[dict, list[str]]:
    """确定性执行受限编辑；非法操作跳过并记录（反馈给 LLM）。"""
    import copy
    prog = copy.deepcopy(program)
    items = prog["items"]
    log = []
    removed = set()
    for e in edits:
        try:
            op = e.get("op")
            if op == "add":
                items.append(e["item_json"])
                log.append("add 一项")
                continue
            i = int(e.get("item", -1))
            if not 0 <= i < len(items) or i in removed:
                log.append(f"跳过：item {i} 不存在")
                continue
            it = items[i]
            if op == "move":
                dx = max(-0.2, min(0.2, float(e.get("dx", 0))))
                dy = max(-0.2, min(0.2, float(e.get("dy", 0))))
                if "at" in it:
                    it["at"] = [round(it["at"][0] + dx, 3), round(it["at"][1] + dy, 3)]
                for key in ("peaks", "plateaus"):   # terrain：山峰与台地随岛一起移动
                    for a in it.get(key) or []:
                        a["at"] = [round(a["at"][0] + dx, 3), round(a["at"][1] + dy, 3)]
                for key in ("path", "points"):
                    if key in it:
                        it[key] = [[round(p[0] + dx, 3), round(p[1] + dy, 3)] for p in it[key]]
            elif op == "scale":
                f = max(0.6, min(1.5, float(e.get("factor", 1))))
                if "tiers" in it:
                    for t in it["tiers"]:
                        t["r"] = round(t["r"] * f, 3)
                if "r" in it:
                    it["r"] = round(it["r"] * f, 3)
                if "size" in it:
                    it["size"] = [round(s * f, 3) for s in it["size"]]
            elif op == "top":
                d = max(-4, min(4, int(e.get("delta", 0))))
                if "tiers" in it:
                    ti = int(e.get("tier", -1))
                    it["tiers"][ti]["top"] = max(1, it["tiers"][ti]["top"] + d)
                elif "top" in it:
                    it["top"] = max(int(it.get("bottom", 0)) + 1, it["top"] + d)
            elif op == "add_tier" and "tiers" in it:
                it["tiers"].append({"r": float(e["r"]), "top": int(e["top"])})
            elif op == "remove":
                removed.add(i)
            else:
                log.append(f"跳过：未知操作 {op}")
                continue
            log.append(f"{op} item {i}")
        except Exception as ex:  # 单条编辑失败不影响其他
            log.append(f"跳过：{str(ex)[:60]}")
    prog["items"] = [it for j, it in enumerate(items) if j not in removed]
    return prog, log
