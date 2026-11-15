"""团队调研：按 TigerMessenger《Mœbius 游戏创作者研究》（docs/MOEBIUS_GAME_ART_STUDY.md）的调研过程，让团队成员分头研究。

调研过程（从那份文档提炼，写进每位成员的提示词，并在代码里做硬约束）
  1. 证据分级：A = 具名作者 / 制作团队直接说出参考或方法（访谈答复属第一手；记者的形容不自动算 A）；
     B = 团队说明的间接影响链，或记者归纳已有采访；C = 仅外观相似，不能据此断言影响、shader 或算法。
     “项目提案”是本项目推断，和作者陈述分开写。
  2. 每条发现标明类型：作者陈述 / 画面观察 / 本项目推断 / 待验证；写来源、日期和实际阅读或观看范围
     （例如“只读官方摘要”“全字幕逐段＋关键帧”），不声称看完没看完的东西。
  3. 每位成员给一个“最小样区”提案：现状证据、所选参考、明确修改点、复用代码 / 现有构件、固定边界、视觉与功能验收。
  4. 列出“尚未覆盖”：缺哪些一手资料、哪些实景基线还没有。
  5. 研究不等于实施授权：产出是调研报告（RESEARCH.md）、待审风格规则（学习版本草稿）和草稿技能，都要人审核后才生效。

本机流水线不联网，资料来自：成员各自的知识文档、Agent 参考文档、你在 Agent 页导入的学习资料、选定的历史创作记录。
"""
from __future__ import annotations

import json
import time
import uuid

from . import config

KINDS = ("作者陈述", "画面观察", "本项目推断", "待验证")
GRADES = ("A", "B", "C", "提案")


def _docs(paths: list[str], limit: int) -> str:
    out = ""
    for rel in paths:
        p = (config.ROOT / rel).resolve()
        if p.is_relative_to(config.ROOT) and p.is_file():
            out += f"\n【{rel}】\n" + p.read_text(errors="ignore")[: max(0, limit - len(out))]
        if len(out) >= limit:
            break
    return out


def _v_member(obj) -> str | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("findings"), list) or not obj["findings"]:
        return "需要 findings 列表"
    for f in obj["findings"]:
        if not isinstance(f, dict) or f.get("kind") not in KINDS or f.get("grade") not in GRADES or not f.get("claim"):
            return f"每条 finding 需要 claim、kind（{'/'.join(KINDS)}）、grade（{'/'.join(GRADES)}）"
    if not isinstance(obj.get("proposal"), dict):
        return "需要 proposal（最小样区）"
    return None


def normalize_findings(findings: list[dict]) -> list[dict]:
    """硬约束：作者陈述必须是 A/B 且写了来源，否则降为待验证；C 级不能当作者陈述；提案只能是本项目推断。"""
    out = []
    for f in findings[:12]:
        f = {k: str(f.get(k, ""))[:400] for k in ("claim", "kind", "grade", "source", "date", "coverage")}
        if f["kind"] == "作者陈述" and (f["grade"] not in ("A", "B") or not f["source"]):
            f["kind"], f["note"] = "待验证", "标为作者陈述但缺 A/B 级来源，降为待验证"
        if f["grade"] == "提案" and f["kind"] != "本项目推断":
            f["kind"] = "本项目推断"
        if f["grade"] == "C" and f["kind"] == "作者陈述":
            f["kind"] = "画面观察"
        out.append(f)
    return out


async def research(ctx) -> None:
    """阶段 team_research：成员分头调研 → 组长汇总结论与主线 → RESEARCH.md + 待审规则 + 草稿技能。"""
    from . import teams, style_agents, agent_config
    from .llm import LLM
    from .pipeline import route
    profile = ctx.options.get("style_profile") or {}
    if not teams.is_team(profile):
        return await style_agents.learn(ctx)
    agent_id = profile["agent_id"]
    agent = style_agents.get(agent_id)
    imported = "\n".join(f"【导入资料：{s.get('title', '')}】{s.get('url', '')}\n{s.get('text', '')[:3000]}"
                         for s in ctx.options.get("learning_sources", []))[:9000]
    runs = []
    for rid in ctx.options.get("learning_runs", [])[:5]:
        p = config.resolve(config.get("pipeline.runs_dir", "runs")) / rid / "RESULTS.md"
        if p.is_file():
            runs.append(f"【历史任务 {rid} 的成果文档】\n" + p.read_text(errors="ignore")[:3000])
    common = (f"研究重点：{ctx.run.get('brief', '')}\n" + _docs(agent.get("references", []), 5000) +
              ("\n" + imported if imported else "") + ("\n" + "\n".join(runs) if runs else ""))
    llm = LLM(route(ctx, "plan"))
    members = []
    for m in teams.roster(profile):
        with teams.speaking(m["id"], "调研：证据分级与最小样区"):
            r = await llm.json([
                {"role": "system", "content": teams._persona(profile, m) + "\n\n调研规范：\n" + __doc__.split("调研过程")[1]
                 .split("本机流水线")[0]},
                {"role": "user", "content": common[:12000] + "\n\n你负责研究：" + m.get("studies", "") +
                 "\n只用上面给出的资料和你学过的文档，不编造作者参数、链接或日期；不知道的写“待验证”。输出 JSON："
                 '{"creator":"人物 / 团队（制作时身份）","game":"作品","evidence":"影响证据等级与一句说明",'
                 '"applies":"本项目最适用部分","findings":[{"claim":"","kind":"作者陈述|画面观察|本项目推断|待验证",'
                 '"grade":"A|B|C|提案","source":"来源（文档名或链接）","date":"","coverage":"实际阅读 / 观看范围"}],'
                 '"proposal":{"area":"样区","current":"现状证据","reference":"所选参考","change":"明确修改点",'
                 '"reuse":"复用的现有构件","boundary":"固定边界（不能动的）","acceptance":"视觉与功能验收"},'
                 '"not_covered":[],"rule":"一条可检查的风格规则","skill":{"name":"","instructions":"做法与验收","stages":["plan"]}}'}],
                validate=_v_member, thinking_budget=2048, on_delta=ctx.delta_emitter())
        rec = {"member": m["id"], "name": m["name"], "studies": m.get("studies", ""),
               **{k: str(r.get(k, ""))[:300] for k in ("creator", "game", "evidence", "applies", "rule")},
               "findings": normalize_findings(r["findings"]),
               "proposal": {k: str((r.get("proposal") or {}).get(k, ""))[:400] for k in
                            ("area", "current", "reference", "change", "reuse", "boundary", "acceptance")},
               "not_covered": [str(x)[:200] for x in (r.get("not_covered") or [])][:6],
               "skill": r.get("skill") if isinstance(r.get("skill"), dict) else {}}
        members.append(rec)
        downgraded = sum(1 for f in rec["findings"] if f.get("note"))
        ctx.emit("team", f"{m['name']} 调研：{len(rec['findings'])} 条发现" + (f"（{downgraded} 条因缺来源降为待验证）" if downgraded else ""),
                 {"phase": "research", "member": m["id"]})
    head = teams.lead(profile)
    with teams.speaking(head["id"], "组长汇总：结论、主线与尚未覆盖"):
        summary = await llm.json([
            {"role": "system", "content": teams._persona(profile, head)},
            {"role": "user", "content": "成员调研：" + json.dumps(members, ensure_ascii=False)[:12000] +
             "\n你是组长，汇总：conclusion（推荐的主线与补充参考，两三句，区分直接影响与间接参考）；"
             "not_covered（全队尚未覆盖的资料与实景基线）。只输出 JSON：{\"conclusion\":\"\",\"not_covered\":[]}"}],
            validate=lambda o: None if isinstance(o, dict) and o.get("conclusion") else "需要 conclusion", thinking_budget=1024)
    report = {"members": members, "conclusion": str(summary.get("conclusion", ""))[:1500],
              "not_covered": [str(x)[:300] for x in summary.get("not_covered", [])][:12], "at": time.time()}
    ctx.state.setdefault("team", {})["research"] = report
    path = ctx.dir / "RESEARCH.md"
    path.write_text(render_markdown(profile, ctx.run, report), encoding="utf-8")
    ctx.artifact("research", path, "团队调研 RESEARCH.md")
    # 产出：待审风格规则（学习版本草稿）+ 草稿技能，都要人在系统 Agent 页 / 技能库审核
    rules = [{"rule": x["rule"], "sources": [f"research:{ctx.id}:{x['member']}"]} for x in members if x["rule"]]
    version = "research-" + uuid.uuid4().hex[:8]
    prof = {"version": version, "agent_id": agent_id, "created": time.time(), "run_id": ctx.id,
            "summary": "团队调研：" + report["conclusion"][:300], "rules": rules[:12],
            "source_ids": [f"research:{ctx.id}"], "sources": [], "based_on": profile.get("version", "baseline"),
            "origin": "team_research"}
    style_agents._save(style_agents._root() / agent_id / "profiles" / f"{version}.json", prof)
    drafted = []
    for x in members:
        sk = x["skill"]
        if sk.get("name") and sk.get("instructions"):
            try:
                drafted.append(agent_config.save_skill({
                    "name": str(sk["name"])[:60], "instructions": str(sk["instructions"])[:4000],
                    "description": f"{x['name']} 调研（{ctx.id}）", "origin": f"research:{ctx.id}",
                    "stages": [s for s in sk.get("stages", []) if s in agent_config.STAGES]})["id"])
            except ValueError:
                pass
    ctx.state["learning"] = {"agent_id": agent_id, "version": version, "summary": prof["summary"], "rules": rules,
                             "skills_drafted": drafted}
    ctx.save()
    ctx.emit("result", f"调研完成：{sum(len(x['findings']) for x in members)} 条分级发现、{len(rules)} 条待审规则、"
             f"{len(drafted)} 条草稿技能；报告 RESEARCH.md。审核应用后才影响新创作。")


def render_markdown(profile: dict, run: dict, report: dict) -> str:
    L = [f"# {run.get('title', '')} · 团队调研（{profile.get('name', '')}）", "",
         f"研究日期：{time.strftime('%Y-%m-%d')}。范围：成员知识文档、Agent 参考资料、导入的学习资料与选定的历史成果；"
         "本机不联网，不把文字资料当动态或实景验收。成员是研究具名创作者公开方法的代理，不是作者本人。", "",
         "## 结论与证据分级", "", report["conclusion"], "",
         "- A：具名作者或制作团队直接说出参考、方法；访谈答复属于第一手陈述，记者自己的形容不自动属于 A。",
         "- B：团队说明间接影响链，或访谈记者归纳已有采访；保留限制。",
         "- C：仅外观相似。不能据此断言作者影响、shader 或生成算法。",
         "- “项目提案”是本项目推断，和上述作者陈述分别书写。", "",
         "## 成员与研究对象", "", "| 成员 | 人物 / 团队 | 作品 | 影响证据 | 本项目最适用部分 |", "|---|---|---|---|---|"]
    cell = lambda t: str(t).replace("|", "／").replace("\n", " ")
    for x in report["members"]:
        L.append(f"| {cell(x['name'])} | {cell(x['creator'])} | {cell(x['game'])} | {cell(x['evidence'])} | {cell(x['applies'])} |")
    for x in report["members"]:
        L += ["", f"### {x['name']}：{x['creator']}", ""]
        for kind in ("作者陈述", "画面观察", "本项目推断", "待验证"):
            rows = [f for f in x["findings"] if f["kind"] == kind]
            if rows:
                L.append(f"**{kind}。** " + " ".join(
                    f"{f['claim']}（{f['grade']}{'；' + f['source'] if f['source'] else ''}{'，' + f['date'] if f['date'] else ''}"
                    f"{'；范围：' + f['coverage'] if f['coverage'] else ''}{'；' + f['note'] if f.get('note') else ''}）" for f in rows))
                L.append("")
    L += ["## 最小样区提案（项目方案，未实施）", "", "| 成员 | 样区 | 现状证据 | 参考 | 修改点 | 复用 | 固定边界 | 验收 |",
          "|---|---|---|---|---|---|---|---|"]
    for x in report["members"]:
        p = x["proposal"]
        L.append("| " + " | ".join(cell(v) for v in (x["name"], p["area"], p["current"], p["reference"], p["change"],
                                                      p["reuse"], p["boundary"], p["acceptance"])) + " |")
    nc = report["not_covered"] + [f"{x['name']}：{n}" for x in report["members"] for n in x["not_covered"]]
    if nc:
        L += ["", "## 尚未覆盖", ""] + [f"- {n}" for n in nc]
    L += ["", "研究不等于实施授权：待审规则与草稿技能需在系统 Agent 页 / 技能库审核后才影响新创作。", ""]
    return "\n".join(L)
