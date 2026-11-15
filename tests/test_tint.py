"""岩体 / 草地的色彩可能性空间（oskar/tint.py，纯 Python）。"""
from genai_pipeline.oskar import palette as OPAL
from genai_pipeline.oskar import tint as OT


def _wall(x, y, z0, z1):
    return [(x, y, z0), (x + 1, y, z0), (x + 1, y, z1), (x, y, z1)]


def test_rock_samples_the_colour_field_not_grey():
    import colorsys
    pal = OPAL.PALETTES["citadel"]
    t = OT.Tinter(pal, seed=3, top=20)
    walls = [t.face("rock", _wall(x, y, z, z + 2.0)) for x in range(-40, 40, 8) for y in (-30, 0, 30)
             for z in (2.0, 6.0, 10.0, 14.0)]
    sat = [colorsys.rgb_to_hsv(*c)[1] for c in walls]
    hues = {round(colorsys.rgb_to_hsv(*c)[0], 1) for c in walls}
    assert sum(sat) / len(sat) > 0.12                       # 不是灰突突：有明确的色相
    assert len(hues) >= 3                                   # 色场里的冷暖漂移（灰绿 / 橙 / 蓝 / 玫瑰）
    low = [t.face("rock", _wall(x, 0, 0.0, 1.0)) for x in range(0, 40, 2)]
    high = [t.face("rock", _wall(x, 0, 16.0, 18.0)) for x in range(0, 40, 2)]
    warm = lambda c: c[0] - c[2]   # noqa: E731
    assert sum(map(warm, high)) / len(high) > sum(map(warm, low)) / len(low)   # 高处暖（橙），低处冷（蓝）


def test_part_colors_cover_tinted_roles_only():
    parts = {"rock": {"v": [[0, 0, 0], [1, 0, 0], [1, 0, 2], [0, 0, 2]], "f": [[0, 1, 2, 3]]},
             "grass": {"v": [[0, 0, 2], [1, 0, 2], [1, 1, 2]], "f": [[0, 1, 2]]},
             "plaster": {"v": [[0, 0, 2], [1, 0, 2], [1, 1, 2]], "f": [[0, 1, 2]]}}
    out = OT.part_colors(parts, OPAL.PALETTES["sea_stack"], seed=1)
    assert set(out) == {"rock", "grass"} and len(out["rock"]) == 1
    


def test_colour_is_laid_top_down_per_peak():
    """纵向铺色：矮岩柱也从自己的山顶走完整条色带（顶暖、脚冷），不是按整座岛的绝对高度。"""
    def column(x, h):
        return {"v": [[x, 0, z] for z in range(0, h + 1, 2) for _ in (0,)], "f": []}
    parts = {"rock": {"v": [], "f": []}}
    for x0, h in ((0, 20), (40, 8)):                       # 一根高岩柱、一根矮岩柱
        for z in range(0, h, 2):
            base = len(parts["rock"]["v"])
            parts["rock"]["v"] += [[x0, 0, z], [x0 + 1, 0, z], [x0 + 1, 0, z + 2], [x0, 0, z + 2]]
            parts["rock"]["f"].append([base, base + 1, base + 2, base + 3])
    cols = OT.part_colors(parts, OPAL.PALETTES["citadel"], seed=2)["rock"]
    tall, short = cols[:10], cols[10:]
    warm = lambda c: c[0] - c[2]   # noqa: E731
    assert warm(tall[-1]) > warm(tall[0]) + 0.05 and warm(short[-1]) > warm(short[0]) + 0.05
