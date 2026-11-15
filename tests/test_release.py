"""先游玩测试，再下载与发布（release.py）。"""
import zipfile

from genai_pipeline import config, release
from genai_pipeline.store import Store


def test_publish_requires_playtest_and_packs_the_project(tmp_path, monkeypatch):
    store = Store(tmp_path / 's.sqlite')
    rid = store.create_run('星球', 'b', 'assembly', {}, ['deliver'])
    run_dir = tmp_path / rid
    proj = run_dir / 'godot_project'
    (proj / 'assets').mkdir(parents=True)
    (proj / 'project.godot').write_text('config_version=5\n')
    (proj / 'assets' / 'oskar_atlas.glb').write_bytes(b'glTF')
    (proj / '.godot').mkdir()
    (proj / '.godot' / 'cache.bin').write_bytes(b'x')
    run = store.get_run(rid)
    assert release.playable(run, proj)
    release.confirm_playtest(store, run)
    assert store.get_run(rid)['options']['playtest']['confirmed']
    monkeypatch.setitem(config.load()['godot'], 'binary', '/nonexistent/godot')
    res = release.publish(store, store.get_run(rid), run_dir, proj)
    assert res['status'] == 'done' and res['files'] and res['notes']          # 没有导出模板：如实说明
    pkg = next((run_dir / 'release').glob('*-godot-project.zip'))
    names = zipfile.ZipFile(pkg).namelist()
    assert 'project.godot' in names and 'README.txt' in names and not any(n.startswith('.godot') for n in names)
    assert store.get_run(rid)['options']['release']['status'] == 'done'
