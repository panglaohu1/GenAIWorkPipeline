"""Compose accepted pipeline outputs into an independent, provenance-pinned game."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
from pathlib import Path

from . import config
from .quality import assess

ROLES = {'level': '关卡场景', 'player': '玩家外观', 'prop': '共享道具'}
MAX_BYTES = 128 * 1024 * 1024


def root():
    return config.resolve(config.get('pipeline.runs_dir', 'runs'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def model_path(value, directory):
    path = config.resolve(str(value)).resolve()
    if not path.is_relative_to(directory.resolve()) or path.suffix.lower() != '.glb' or not path.is_file():
        raise ValueError('模型文件不可用或不在原任务目录内')
    size = path.stat().st_size
    if not 20 <= size <= MAX_BYTES:
        raise ValueError('单个 GLB 必须为 20 字节至 128 MB')
    with path.open('rb') as f:
        header = f.read(12)
    if header[:4] != b'glTF' or struct.unpack('<II', header[4:]) != (2, size):
        raise ValueError('GLB 文件头或长度无效')
    return path


def source(store, run_id):
    run = store.get_run(run_id)
    if not run or run['status'] != 'done' or run['mode'] not in ('asset', 'oskar', 'scene', 'scene3d'):
        raise ValueError('仅可选择已完成的资产或场景产线')
    directory = root() / run_id
    state = json.loads((directory / 'state.json').read_text())
    quality = assess(run, state)
    if quality['status'] != 'passed':
        raise ValueError('原产线尚未通过已有验收')
    spec = state.get('spec') or {}
    assets = spec.get('assets') or []
    if run['mode'] in ('scene', 'scene3d'):
        items = [{'id': 'scene', 'name': run['title'], 'glb': (state.get('compose') or {}).get('glb')}]
    else:
        items = [{**a, 'glb': (state.get('models', {}).get(a['id']) or {}).get('glb')} for a in assets]
    if not items or len(items) > 24:
        raise ValueError('产线需要 1–24 个可用模型')
    for item in items:
        item['glb'] = str(model_path(item.get('glb'), directory))
    return run, state, quality, items


def catalog(store):
    accepted, excluded = [], 0
    for run in store.list_runs(10000):
        if run['mode'] not in ('asset', 'oskar', 'scene', 'scene3d'):
            continue
        try:
            run, state, quality, items = source(store, run['id'])
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            excluded += 1
            continue
        images = [a for a in store.artifacts(run['id']) if a['kind'] in ('render', 'screenshot')
                  and Path(a['path']).is_file()]
        accepted.append({'id': run['id'], 'title': run['title'], 'mode': run['mode'],
                         'default_role': 'level' if run['mode'] in ('oskar', 'scene', 'scene3d') else 'prop',
                         'model_count': len(items), 'bytes': sum(Path(a['glb']).stat().st_size for a in items),
                         'preview': images[-1]['id'] if images else None,
                         'quality': quality['label'], 'created': run['created']})
    return {'items': accepted, 'excluded': excluded, 'roles': ROLES}


FORMS = {'levels': '关卡串联', 'planet': '星球'}


def prepare(store, title, brief, selections, form='levels'):
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise ValueError('请填写游戏名称（最多 120 字）')
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 4000:
        raise ValueError('请填写游戏目标与串联要求（最多 4000 字）')
    if not isinstance(selections, list) or not 1 <= len(selections) <= 12:
        raise ValueError('请选择 1–12 条产线')
    seen, parts = set(), []
    for entry in selections:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or entry.get('role') not in ROLES:
            raise ValueError('请选择有效的产线和用途')
        if entry['id'] in seen:
            raise ValueError('同一产线不能重复选择')
        seen.add(entry['id'])
        goal = entry.get('goal', '')
        if not isinstance(goal, str) or len(goal) > 600:
            raise ValueError('每个关卡目标最多 600 字')
        before = digest(root()/entry['id']/'state.json') if store.get_run(entry['id']) else ''
        run, state, quality, items = source(store, entry['id'])
        if digest(root()/entry['id']/'state.json') != before:
            raise ValueError('原任务状态已改变，请刷新成果库')
        if entry['role'] == 'player' and len(items) != 1:
            raise ValueError('玩家外观必须来自单模型产线')
        part = {'source_id': run['id'], 'title': run['title'], 'mode': run['mode'],
                'role': entry['role'], 'goal': goal.strip(), 'quality': quality,
                'state_sha256': before, 'assets': items}
        program = (state.get('oskar') or {}).get('program')
        if run['mode'] == 'oskar' and isinstance(program, dict) and program.get('items'):
            part['program'] = program   # 星球组装用它重建场景（可站立点、通路、当前山体色场）
        parts.append(part)
    if not any(p['role'] == 'level' for p in parts):
        raise ValueError('至少选择一个关卡场景')
    if sum(p['role'] == 'player' for p in parts) > 1:
        raise ValueError('只能设置一个玩家外观')
    if form not in FORMS:
        raise ValueError('组装方式只能是关卡串联或星球')
    if form == 'planet' and sum(p['role'] == 'level' for p in parts) < 2:
        raise ValueError('星球组装至少选择两个关卡场景')
    if sum(Path(a['glb']).stat().st_size for p in parts for a in p['assets']) > 512 * 1024 * 1024:
        raise ValueError('本次组装模型总量不能超过 512 MB')
    return {'version': 1, 'title': title.strip(), 'brief': brief.strip(), 'parts': parts, 'form': form}


def freeze(manifest, directory):
    inputs = directory / 'assembly-inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    for i, part in enumerate(manifest['parts'], 1):
        for j, asset in enumerate(part['assets'], 1):
            original = Path(asset['glb'])
            fingerprint = digest(original)
            asset['source_asset_id'] = asset['id']
            asset['id'] = f'part_{i:02d}_{j:02d}'
            target = inputs / f"{asset['id']}.glb"
            shutil.copy2(original, target)
            asset['sha256'] = digest(target)
            if fingerprint != asset['sha256'] or fingerprint != digest(original):
                raise ValueError('复制期间原模型改变，请刷新成果库后重试')
            asset['glb'] = str(target)
    for part in manifest['parts']:
        if digest(root()/part['source_id']/'state.json') != part['state_sha256']:
            raise ValueError('复制期间原任务状态改变，请刷新成果库后重试')
    (directory/'assembly-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def validate(options, directory):
    manifest = options.get('assembly')
    if not manifest:
        return
    if not manifest.get('parts'):
        raise ValueError('游戏组装清单为空')
    for part in manifest['parts']:
        if part['quality']['status'] != 'passed':
            raise ValueError('来源验收快照无效')
        for asset in part['assets']:
            path = model_path(asset['glb'], directory/'assembly-inputs')
            if digest(path) != asset['sha256']:
                raise ValueError('组装模型快照改变，请重新创建组装任务')


async def plan(ctx):
    manifest = ctx.options['assembly']
    validate(ctx.options, ctx.dir)
    exports = ctx.dir/'blender'/'export'
    exports.mkdir(parents=True, exist_ok=True)
    assets, levels, shared = [], [], []
    for part in manifest['parts']:
        ids = []
        for asset in part['assets']:
            target = exports/f"{asset['id']}.glb"
            shutil.copy2(asset['glb'], target)
            assets.append({**asset, 'role': ROLES[part['role']], 'description': f"来自 {part['title']}；保留模型外观"})
            ctx.state.setdefault('models', {})[asset['id']] = {'glb': str(target), 'source_id': part['source_id']}
            ctx.artifact('glb', target, asset['name'], asset=asset['id'])
            ids.append(asset['id'])
        item = {'title': part['title'], 'assets': ids, 'goal': part['goal'] or '到达出口进入下一关', 'role': part['role']}
        (levels if part['role'] == 'level' else shared).append(item)
    ctx.state['assembly'] = {'sources_verified': True, 'journey_approved': False,
                             'form': manifest.get('form', 'levels')}
    ctx.state.pop('final_review', None)
    ctx.state['spec'] = {'title': manifest['title'], 'assets': assets, 'camera': '第三人称跟随',
        'controls': 'WASD 移动，空格跳跃', 'core_loop': manifest['brief'],
        'scene': {'ordered_levels': levels, 'shared_assets': shared},
        'gameplay': '按 ordered_levels 顺序切换独立关卡，显示关卡名和目标；完成目标后触发出口，进入下一关。'
                    '最后一关完成后显示通关与重新开始。共享玩家与道具跨关复用；没有玩家模型时使用 Kit 默认玩家。'
                    '每关必须有安全出生点、可通行地面和碰撞。保留传入 GLB，不重新生成资产。',
        'acceptance': ['所有已选模型按指定用途出现在游戏中', '关卡顺序与组装清单一致',
                       '玩家可以移动和跳跃，没有脚本或运行错误', '目标、出口、切换关卡、通关与重开可执行']}
    ctx.save()
    ctx.emit('result', f'已固定 {len(manifest["parts"])} 条产线，编排 {len(levels)} 个关卡与 {len(shared)} 个共享模块')


async def journey(ctx):
    ctx.state.setdefault('assembly', {})['journey_approved'] = False
    ctx.save()
    unresolved = [c for c in assess(ctx.run, ctx.state)['issues'] if c['stage'] != 'journey']
    if unresolved:
        raise RuntimeError('自动验收尚未通过，请先修复：' + '；'.join(c['name'] for c in unresolved))
    # Game movement/visual tests alone cannot verify all transitions and victory.
    gates = list(ctx.options.get('approval_gates') or [])
    ctx.options['approval_gates'] = list(dict.fromkeys(gates + ['journey']))
    result = await ctx.wait_approval('journey')
    if result.get('selections', {}).get('journey_played') is not True:
        raise ValueError('需人工试玩全部关卡、目标、出口、通关与重开后确认')
    ctx.state['assembly']['journey_approved'] = True
    ctx.save()


def validate_delivery(ctx):
    if ctx.run['mode'] != 'assembly':
        return
    validate(ctx.options, ctx.dir)
    project = ctx.dir/'godot_project'
    if not (project/'project.godot').is_file():
        raise ValueError('缺少游戏工程，不能交付')
    if ctx.options['assembly'].get('form') == 'planet':   # 星球：场景已重建并整合成一个 GLB
        if not (project/'assets'/'oskar_atlas.glb').is_file():
            raise ValueError('缺少星球模型，请从星球整合重试')
        return
    for part in ctx.options['assembly']['parts']:
        for asset in part['assets']:
            for path in (ctx.dir/'blender'/'export'/f"{asset['id']}.glb",
                         project/'assets'/f"{asset['id']}.glb"):
                if not path.is_file() or digest(path) != asset['sha256']:
                    raise ValueError('组装使用的模型与固定版本不一致，请从成果编排重试')
