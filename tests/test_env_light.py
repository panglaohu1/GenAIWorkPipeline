"""天空盒、PBR 贴图、布光闭环的纯函数测试。"""
import numpy as np

from genai_pipeline import envgen as G
from genai_pipeline.pipeline import _rig_init, _rig_step


def test_sky_dome_interpolates_measured_profiles():
    d = G.sky_dome([(0, [0.9, 0.95, 0.97]), (30, [0.6, 0.85, 0.96])], [(-20, [0.15, 0.4, 0.55]), (0, [0.7, 0.8, 0.85])], 64, 32)
    top, bottom = G.l2s(d[0, 0]), G.l2s(d[-1, 0])
    assert abs(top[0] - 0.6) < 0.02 and abs(bottom[0] - 0.15) < 0.02
    assert np.allclose(d[:, 0], d[:, -1])          # 方位均匀：左右无缝


def test_row_angles_follow_camera_pitch():
    a = G.row_angles(101, {"fov_deg": 60, "aspect": 1.0, "pitch_down_deg": 10})
    assert abs(a[50] + 10) < 0.01 and a[0] > 0 > a[-1]


def test_tileable_and_pbr_maps():
    x = np.linspace(0, 1, 96, dtype=np.float32)
    img = np.stack([np.repeat(x[None], 96, 0)] * 3, -1)
    t = G.tileable(img)
    assert np.abs(t[:, 0] - t[:, -1]).mean() < 0.02
    m = G.pbr_maps(t)
    assert set(m) == {"albedo", "height", "normal", "roughness", "ao", "metallic"}
    assert m["normal"].shape == (96, 96, 3) and 0 <= m["roughness"].min() <= m["roughness"].max() <= 1


def test_seam_wrap_closes_panorama():
    rng = np.random.default_rng(0)
    img = rng.random((32, 64, 3)).astype(np.float32)
    assert np.abs(G.seam_wrap(img)[:, 0] - G.seam_wrap(img)[:, -1]).max() < 1e-5


def test_hdr_roundtrip_header(tmp_path):
    p = G.save_hdr(str(tmp_path / "a.hdr"), np.full((4, 8, 3), 2.0, np.float32))
    head = open(p, "rb").read(64)
    assert head.startswith(b"#?RADIANCE") and b"-Y 4 +X 8" in head


def test_rig_step_raises_key_when_render_is_flat():
    spec = {"light_measured": {"dir_world": [-0.3, 0.2, 0.9]},
            "palette_measured": {"sky": {"base": "#A1DFF6"}}}
    lt = {"k": [1.3, 1.74, 1.44], "a": [0.575, 0.241, 0.164]}
    rig = _rig_init(spec, lt)
    assert rig["key"]["dir"] == [-0.3, 0.2, 0.9] and max(rig["key"]["color"]) == 1.0
    flat = {"k": [0.05, 0.06, 0.05], "a": [0.57, 0.24, 0.16]}
    new, err = _rig_step(rig, lt, flat)
    assert err > 1.0 and new["key"]["energy"] > rig["key"]["energy"] * 2
    same, err2 = _rig_step(rig, lt, lt)
    assert err2 < 1e-6 and abs(same["key"]["energy"] - rig["key"]["energy"]) < 1e-6


def test_lambert_fit_recovers_key_ratio():
    from genai_pipeline.color_palette import lambert_fit, _lin
    rng = np.random.default_rng(0)
    n = rng.normal(size=(64, 64, 3)); n[..., 2] = -np.abs(n[..., 2]); n /= np.linalg.norm(n, axis=-1, keepdims=True)
    lc = np.array([0.0, -1.0, 0.0])
    alb = np.array([0.6, 0.3, 0.2]); amb, key = 0.5, 0.8
    lin = alb * (amb + key * np.clip(n @ lc, 0, None))[..., None]
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    f = lambert_fit(srgb.astype(np.float32), n, np.ones((64, 64), bool), lc)
    assert all(abs(k - key / amb) < 0.05 for k in f["k"])


def test_lambert_fit_ignores_clipped_highlights():
    from genai_pipeline.color_palette import lambert_fit
    rng = np.random.default_rng(1)
    n = rng.normal(size=(96, 96, 3)); n[..., 2] = -np.abs(n[..., 2]); n /= np.linalg.norm(n, axis=-1, keepdims=True)
    lc = np.array([0.0, -1.0, 0.0])
    alb = np.array([0.9, 0.45, 0.3]); amb, key = 0.6, 0.9   # 红色亮面会顶到 1
    lin = np.clip(alb * (amb + key * np.clip(n @ lc, 0, None))[..., None], 0, 1)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    assert (srgb[..., 0] >= 0.999).mean() > 0.05
    f = lambert_fit(srgb.astype(np.float32), n, np.ones((96, 96), bool), lc)
    assert all(abs(k - key / amb) < 0.1 for k in f["k"]), f["k"]


def test_audio_loop_crossfade_is_seamless():
    import numpy as np
    from genai_pipeline.audio_server import make_loop, _write_wav
    rate = 1000
    t = np.arange(4000) / rate
    a = np.sin(2 * np.pi * 3.3 * t).astype(np.float32) + np.linspace(0, 1, 4000, dtype=np.float32)
    lp = make_loop(a, rate, fade=0.5)
    assert lp.shape[0] == 4000 - 500
    # the wrap point (last → first sample) is as smooth as an ordinary step
    step = np.abs(np.diff(a)).max()
    assert abs(float(lp[0]) - float(lp[-1])) < 3 * step
    st = make_loop(np.stack([a, a], 1), rate)
    assert st.shape == (3500, 2)


def test_audio_wav_writer(tmp_path):
    import numpy as np, wave
    from genai_pipeline.audio_server import _write_wav
    p = tmp_path / "x.wav"
    dur = _write_wav(str(p), np.ones((2, 480), np.float32) * 2, 480)
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, 480, 480)
    assert dur == 1.0


def test_export_environment_writes_rig(tmp_path):
    from types import SimpleNamespace
    from genai_pipeline.pipeline import _export_environment
    sky = tmp_path / "sky.png"; sky.write_bytes(b"png")
    ctx = SimpleNamespace(state={"compose": {"sky_dome": str(sky), "light_rig": {"key": {"energy": 3}}, "light_err": 0.06},
                                 "spec": {"light_fit": {"k": [1, 1, 1]}}})
    lines = _export_environment(ctx, tmp_path / "out")
    import json
    rig = json.loads((tmp_path / "out/environment/light_rig.json").read_text())
    assert rig["rig"]["key"]["energy"] == 3 and (tmp_path / "out/environment/sky_dome.png").exists()
    assert any("light_rig.json" in l for l in lines)
    assert _export_environment(SimpleNamespace(state={}), tmp_path / "o2") == []


def test_color_fix_step_tolerates_unstriped_backdrop_materials():
    from genai_pipeline.pipeline import _color_fix_step
    mats = [{"root": "sea_1", "material": m, "px": 500, "authored": [0.5, 0.5, 0.5], "rendered": [0.4, 0.5, 0.6]}
            for m in ("sea_water", "sea_sky", "sea_hills")]
    mats.append({"root": "rock_1", "material": "rock", "px": 900, "authored": [0.6, 0.4, 0.3], "rendered": [0.5, 0.4, 0.3]})
    new, info = _color_fix_step(mats, {"sea_1": "sea", "rock_1": "rock"}, {}, {"sea"})
    assert "rock" in new and "sea" in new
