"""OPC 与团队两种 Agent 形态；议事（ORID + 五指共识）、分工注入、token 记账、复盘结算与棘轮（替身 LLM）。"""
import json
import pytest

from genai_pipeline import style_agents, teams, pipeline
from genai_pipeline.agent import RunControl
from genai_pipeline.pipeline import Orchestrator, RunContext, STAGES
from genai_pipeline.store import Store


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(style_agents, '_root', lambda: tmp_path / 'agents')
    return tmp_path / 'agents'


def test_two_agent_forms_and_team_snapshot(registry):
    oskar, greg = style_agents.get('oskar'), style_agents.get('greg_team')
    assert teams.kind(oskar) == 'opc' and teams.kind(greg) == 'team'
    snap = style_agents.snapshot('greg_team')
    assert snap['kind'] == 'team' and len(snap['members']) == 6 and snap['lead'] == 'greg_lead'
    assert teams.lead(snap)['name'] == 'Greg'
    owner, reviewers = teams.stage_owner(snap, 'concept')
    assert owner['id'] == 'greg_visual' and 'greg_lead' in [r['id'] for r in reviewers]
    text = style_agents.rules_text(snap, 'assemble')
    assert '负责人：有机通行' in text and '评审' in text
    assert teams.stage_rules(style_agents.snapshot('oskar'), 'plan') == ''
    assert all('instructions' not in m for m in teams.public_roster(greg))


def test_team_submission_gets_plaza_retro_and_plan_gate(registry, tmp_path):
    store = Store(tmp_path / 's.sqlite')
    orch = Orchestrator(store, start_worker=False)
    rid = orch.submit('湖沼', '一个莫比斯风格的湖沼小游戏', 'game', {'style_agent': 'greg_team'}, start=False)
    run = store.get_run(rid)
    assert run['mode'] == 'game' and [s['name'] for s in store.stages(rid)] == STAGES['team_game']
    assert run['options']['approval_gates'] == ['team_plaza']
    rid2 = orch.submit('岛', '岛', 'game', {'style_agent': 'oskar'}, start=False)
    assert 'team_plaza' not in [s['name'] for s in store.stages(rid2)]


def test_consensus_rules():
    v = lambda *f: [{'member': str(i), 'fingers': x} for i, x in enumerate(f)]
    assert teams.consensus(v(5, 4, 3, 3))['reached']
    assert teams.consensus(v(5, 5, 1))['level'] == 'blocked'
    assert not teams.consensus(v(2, 2, 3))['reached']


def test_ratchet_only_moves_forward(registry):
    assert teams.advance('greg_team', 'efficiency', 0.5, {})['generation'] == 1
    r = teams.advance('greg_team', 'efficiency', 0.4, {})
    assert not r['advanced'] and '只进不退' in r['reason']
    assert teams.advance('greg_team', 'efficiency', 0.6, {})['generation'] == 2
    assert teams.ratchet('greg_team')['metrics']['efficiency']['value'] == 0.6


class FakeLLM:
    calls = []

    def __init__(self, key='main'):
        pass

    async def json(self, messages, validate=None, **kw):
        sysmsg = messages[0]['content']
        user = messages[-1]['content'] if isinstance(messages[-1]['content'], str) else messages[-1]['content'][0]['text']
        who = sysmsg.split('「')[1].split('」')[0]
        FakeLLM.calls.append(who)
        teams.record_usage({'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150})
        if '写总评' in user:
            obj = {'summary': '计划已执行，实景未看', 'unfinished': [{'priority': 'P0', 'item': '入口过陡', 'hours': '0.5–1', 'exit': '往返通过'}]}
        elif '五指投票' in user:
            obj = {'fingers': 1 if who == '有机通行' and len([c for c in FakeLLM.calls if c == '有机通行']) == 2 else 4,
                   'reason': 'ok', 'change': '桥下净空 ≥ 2.2'}
        elif '组长' in user or '修订' in user:
            obj = {'goal': '月夜湖沼', 'scale': '人 1.7 m', 'landmarks': '世界树', 'palette': [],
                   'phases': [{'stage': s, 'owner': o, 'reviewers': ['greg_lead'], 'tasks': [f'{s} 任务'],
                               'acceptance': [f'{s} 验收']} for s, o in
                              [('plan', 'greg_lead'), ('concept', 'greg_visual'), ('model', 'greg_ecology'),
                               ('assemble', 'greg_paths'), ('review', 'greg_lead')]],
                   'risks': [], 'open_questions': []}
        elif '复盘' in user:
            obj = {'score': 7, 'went_well': ['x'], 'issues': [], 'next_rule': f'{who} 的规则', 'learned': [f'{who} 学到的'],
                   'deliverables': [{'item': f'{who} 的交付', 'status': '实景已看', 'evidence': '编造的截图'},
                                    {'item': '设计稿', 'status': '已设计', 'evidence': ''}]}
        else:
            obj = {'facts': ['f'], 'risks': ['r'], 'proposals': [f'{who} 提案'], 'acceptance': ['a']}
        assert validate is None or validate(obj) is None, (who, obj)
        return obj


@pytest.mark.asyncio
async def test_plaza_plan_votes_revision_tokens_and_retro(registry, tmp_path, monkeypatch):
    from genai_pipeline import config, llm as L
    monkeypatch.setitem(config.load()['pipeline'], 'runs_dir', str(tmp_path / 'runs'))
    monkeypatch.setattr(L, 'LLM', FakeLLM)
    monkeypatch.setattr(pipeline, 'route', lambda ctx, role: 'main')
    store = Store(tmp_path / 's.sqlite')
    orch = Orchestrator(store, start_worker=False)
    rid = orch.submit('湖沼', '湖沼', 'game', {'style_agent': 'greg_team', 'approval_gates': []}, start=False)
    store.update_run(rid, options={**store.get_run(rid)['options'], 'approval_gates': ['x']})   # 测试不停在关口
    ctx = RunContext(store, store.get_run(rid), RunControl())
    ctx.stage = 'team_plaza'
    with teams.attribution(ctx, 'team_plaza'):
        await teams.plaza(ctx)
    room = ctx.state['team']
    assert len(room['speeches']) == 6 and room['plan']['goal'] == '月夜湖沼'
    assert room['revisions'] == 1 and room['consensus']['reached'] is False or room['revisions'] == 1
    # 计划写进快照：之后的阶段提示词按计划分工
    snap = store.get_run(rid)['options']['style_profile']
    assert snap['team_plan']['phases'][2]['owner'] == 'greg_ecology'
    assert 'model 任务' in style_agents.rules_text(snap, 'model') and '异星生态' in style_agents.rules_text(snap, 'model')
    subs = ctx.state['subagents']
    assert all(x['status'] == 'done' for x in subs) and sum(x['task'].startswith('议事发言') for x in subs) == len(room['speeches'])
    assert any(x['task'] == '组长成稿：结构化执行计划' and x['member'] == 'greg_lead' and x['tokens'] == 150 for x in subs)
    tok = ctx.state['tokens']
    assert tok['total'] == 150 * len(FakeLLM.calls) and set(tok['by_member']) >= {'greg_lead', 'greg_paths'}
    # 复盘：6 人打分 + 结算 + 棘轮第一代 → 待审规则
    ctx.options = store.get_run(rid)['options']
    ctx.stage = 'team_retro'
    with teams.attribution(ctx, 'team_retro'):
        await teams.retro(ctx)
    st = ctx.state['settlement']
    assert st['kind'] == 'team' and st['ratchet']['advanced'] and st['tokens'] == tok['total']
    room = ctx.state['team']
    assert room['review']['unfinished'][0]['priority'] == 'P0'
    assert room['retro'][0]['deliverables'][0]['status'] == '未验收（证据不符）'   # 不虚报
    res_md = (ctx.dir / 'RESULTS.md').read_text()
    assert '## 每个人做了什么' in res_md and '尚未完成及下一轮安排' in res_md and 'Greg 学到的' in res_md
    pend = teams.pending_rules('greg_team')[0]
    assert len(pend['rules']) == 6
    prof = next(p for p in style_agents.profiles('greg_team') if p['version'] == pend['version'])
    assert prof['origin'] == 'team_retro' and len(prof['rules']) == 6 and style_agents.snapshot('greg_team')['version'] == 'baseline'


# ───────────── Agent 配置与技能库（AgentsGroup2026 引入） ─────────────
from genai_pipeline import agent_config as AC


def test_config_overlay_member_settings_and_snapshot(registry):
    cfg = AC.get_config('greg_team')
    assert cfg['autonomy_level'] == 2 and AC.get_config('oskar')['autonomy_level'] == 4
    assert set(cfg['members']) == {'greg_lead', 'greg_visual', 'greg_ecology', 'greg_paths', 'greg_habitation', 'greg_place'}
    AC.save_config('greg_team', {'personality': {'tone': '温和', 'creativity': 0.9}, 'token_budget': 50000,
                                 'members': {'greg_place': {'enabled': False}, 'greg_visual': {'model': 'main',
                                             'extra_instructions': '线色不用纯黑'}}})
    with pytest.raises(ValueError):
        AC.save_config('greg_team', {'members': {'greg_lead': {'enabled': False}}})
    with pytest.raises(ValueError):
        AC.save_config('greg_team', {'skills': ['nope']})
    snap = style_agents.snapshot('greg_team')
    assert [m['id'] for m in snap['members']].count('greg_place') == 0 and len(snap['members']) == 5
    vis = teams.member(snap, 'greg_visual')
    assert vis['model'] == 'main' and '线色不用纯黑' in vis['instructions']
    assert snap['config']['token_budget'] == 50000
    text = style_agents.rules_text(snap, 'concept')
    assert '温和' in text and '大胆尝试' in text and '语义色板' in text   # 人格 + 本阶段负责人（视觉规则）的技能


def test_skill_router_respects_stage_member_and_tools(registry):
    snap = style_agents.snapshot('greg_team')
    picked = [s['id'] for s in AC.route_skills(snap, 'assemble', '湖沼 路径 净空', 'greg_paths')]
    assert picked[0] == 'sk-real-support' and 'sk-root-stem-crown' not in picked   # 生态成员的技能不给通行阶段
    AC.save_config('greg_team', {'tools': ['flux']})   # 不给 Godot：依赖 Godot 的技能不参与路由
    snap = style_agents.snapshot('greg_team')
    assert 'sk-real-support' not in [s['id'] for s in AC.route_skills(snap, 'assemble', '路径', 'greg_paths')]
    oskar = style_agents.snapshot('oskar')
    assert AC.route_skills(oskar, 'oskar_build', '岩体 配色')[0]['id'] in ('sk-oskar-vertical-colour', 'sk-oskar-big-form')


def test_skill_crud_lifecycle_and_extraction(registry):
    s = AC.save_skill({'name': '湖沼浅滩分层', 'instructions': '浅滩 0.3 m、中水 1 m、深水 2 m 三层色。', 'stages': ['model']})
    assert s['lifecycle'] == 'draft' and s['source'] == 'user'
    assert AC.set_lifecycle(s['id'], 'publish')['lifecycle'] == 'published'
    for ok in (True, True, True):
        AC.settle_skills('greg_team', 'r', {s['id']: {}}, ok)
    assert AC.get_skill(s['id'])['lifecycle'] == 'verified'
    for _ in range(5):
        AC.settle_skills('greg_team', 'r', {s['id']: {}}, False)
    assert AC.get_skill(s['id'])['lifecycle'] == 'degraded'
    with pytest.raises(ValueError):
        AC.save_skill({'id': 'sk-sable-blocks', 'name': 'x', 'instructions': 'y'})   # 内置技能正文不能改
    with pytest.raises(ValueError):
        AC.delete_skill('sk-sable-blocks')
    AC.settle_skills('oskar', 'r9', {'sk-sable-blocks': {}}, True)   # 内置技能统计写数据目录
    assert AC.get_skill('sk-sable-blocks')['usage'] == 1 and 'oskar' in AC.get_skill('sk-sable-blocks')['adopted_by']
    made = AC.extract_from_retro('greg_team', 'run7', ['桥下净空至少 2.2 米', ''])
    assert len(made) == 1 and made[0]['lifecycle'] == 'draft' and made[0]['origin'] == 'retro:run7'
    AC.delete_skill(made[0]['id'])
    assert all(x['id'] != made[0]['id'] for x in AC.list_skills())


def test_autonomy_sets_default_gates(registry, tmp_path):
    store = Store(tmp_path / 's.sqlite')
    orch = Orchestrator(store, start_worker=False)
    AC.save_config('oskar', {'autonomy_level': 1, 'image_backend': 'flux'})
    rid = orch.submit('岛', '岛', 'game', {'style_agent': 'oskar'}, start=False)
    opts = store.get_run(rid)['options']
    assert opts['approval_gates'] == ['concept'] and opts['image_backend'] == 'flux'


def test_research_normalizes_evidence_grades():
    from genai_pipeline import team_research as R
    out = R.normalize_findings([
        {'claim': 'Kythreotis 说参考了 Mœbius', 'kind': '作者陈述', 'grade': 'A', 'source': 'cookandbecker 2019'},
        {'claim': '记者说影响来自 Mœbius', 'kind': '作者陈述', 'grade': 'B', 'source': ''},
        {'claim': '看起来像', 'kind': '作者陈述', 'grade': 'C', 'source': 'x'},
        {'claim': '湖沼三层岸台', 'kind': '画面观察', 'grade': '提案'}])
    assert [f['kind'] for f in out] == ['作者陈述', '待验证', '待验证', '本项目推断']


@pytest.mark.asyncio
async def test_team_learning_runs_research_and_writes_report(registry, tmp_path, monkeypatch):
    from genai_pipeline import config, llm as L, team_research as R, agent_config
    monkeypatch.setitem(config.load()['pipeline'], 'runs_dir', str(tmp_path / 'runs'))
    monkeypatch.setattr(pipeline, 'route', lambda ctx, role: 'main')

    class RL:
        def __init__(self, key='main'): pass

        async def json(self, messages, validate=None, **kw):
            user = messages[-1]['content']
            who = messages[0]['content'].split('「')[1].split('」')[0]
            if '汇总' in user:
                obj = {'conclusion': '以 Sable 空间与地标为主线', 'not_covered': ['GDC 完整音画']}
            else:
                obj = {'creator': who + '研究对象', 'game': 'Sable', 'evidence': 'A', 'applies': '尺度',
                       'findings': [{'claim': '先空间后线条', 'kind': '作者陈述', 'grade': 'A', 'source': 'GDC 2022',
                                     'date': '2022', 'coverage': '全字幕'},
                                    {'claim': '没来源的陈述', 'kind': '作者陈述', 'grade': 'A', 'source': ''}],
                       'proposal': {'area': '湖沼一段岸线', 'acceptance': '同机位近中远'}, 'not_covered': [],
                       'rule': f'{who} 的规则', 'skill': {'name': f'{who} 技能', 'instructions': '做法', 'stages': ['plan']}}
            assert validate is None or validate(obj) is None
            return obj
    monkeypatch.setattr(L, 'LLM', RL)
    store = Store(tmp_path / 's.sqlite')
    orch = Orchestrator(store, start_worker=False)
    rid = orch.submit('调研', '研究湖沼的生态与通行', 'learn', {'style_agent': 'greg_team'}, start=False)
    assert [s['name'] for s in store.stages(rid)] == ['team_research']
    ctx = RunContext(store, store.get_run(rid), RunControl())
    ctx.stage = 'team_research'
    with teams.attribution(ctx, 'team_research'):
        await R.research(ctx)
    rep = ctx.state['team']['research']
    assert len(rep['members']) == 6 and rep['members'][0]['findings'][1]['kind'] == '待验证'
    md = (ctx.dir / 'RESEARCH.md').read_text()
    assert '## 结论与证据分级' in md and '## 最小样区提案' in md and '尚未覆盖' in md
    assert len(ctx.state['learning']['skills_drafted']) == 6
    assert all(agent_config.get_skill(i)['lifecycle'] == 'draft' for i in ctx.state['learning']['skills_drafted'])
    assert any(p['origin'] == 'team_research' for p in style_agents.profiles('greg_team'))
