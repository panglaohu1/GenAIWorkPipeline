"""语义色角色 → sRGB 色值。起点取自 oskar-world-advisor 的待验证建议与目标图，需同机位验收后再定稿。"""

PALETTES = {
    # 十二门徒：冷蓝灰层状石灰岩、低饱和橄榄植被、青绿海水与白色泡沫
    "sea_stack": {
        "rock": "#98A0A6", "rock_alt": "#B1B6B7", "rock_dark": "#5B636B", "rock_top": "#A7ACA8",
        "grass": "#66744F", "shrub": "#56673F", "sea": "#3A93A0", "foam": "#E6F0F1",
        "stone": "#B7AE9C", "stone_alt": "#C9C1AF", "plaster": "#DCD2BE", "roof": "#925D4D", "dome": "#586D73",
        "window": "#3A3F4A", "door": "#6B4A34", "trim": "#6F6A60", "cypress": "#3E5A36",
        "outline": "#2B2F38", "sky": "#9FB3C4", "water": "#D3ECF0", "sea_deep": "#1F5E6B",
        "crystal": "#C4E6F4", "crystal_alt": "#93C7E2", "metal": "#B8894A", "trunk": "#6B5238", "canopy": "#5E7A45",
    },
    # 高山圣城旧城（地中海宫城）：暖象牙石、奶油灰泥、浅金穹顶、少量赭瓦、灰岩崖体、深绿柏树、青蓝内湾
    "citadel": {
        "rock": "#A2A6A5", "rock_alt": "#B8BBB8", "rock_dark": "#666B6C", "rock_top": "#A9AC9F",
        "grass": "#6F8C48", "shrub": "#56793C", "sea": "#2E98B5", "foam": "#EEF5F6",
        "stone": "#D3C7AA", "stone_alt": "#E2D8C0", "plaster": "#EBE0C8", "roof": "#A5654E", "dome": "#D9B97E",
        "window": "#3B3D46", "door": "#6B4A34", "trim": "#B3A27F", "cypress": "#2E4A2B",
        "outline": "#2E2C2A", "sky": "#A9C4DA", "water": "#D7EEF2", "sea_deep": "#1D5E77",
        "crystal": "#C4E6F4", "crystal_alt": "#93C7E2", "metal": "#B8894A", "trunk": "#6B5238", "canopy": "#56793C",
    },
}


def hex_to_linear(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


# ───────────────────────── 自动配色：从目标图按语义区域取色 ─────────────────────────
# SAM 3 文本提示 → 取该区域中位色 → 派生亮面 / 暗面 / 描边。没分到的角色沿用基础调色盘。
AUTO_ROLES = {"rock": ["rock", "cliff", "mountain"], "sea": ["water", "sea", "lake"], "plaster": ["building", "wall"],
              "roof": ["roof"], "dome": ["dome"], "canopy": ["tree"], "grass": ["grass", "bush"],
              "crystal": ["crystal tower", "crystal"]}
AUTO_PROMPTS = sorted({p for ps in AUTO_ROLES.values() for p in ps})


def _hex(c) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(x))) for x in c)


def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def region_colors(image: str, seg: dict) -> dict[str, str]:
    """seg = {prompt: [{"mask": png, "area", "score"}]} → {角色: 中位色 hex}；每个角色合并得分最高的 3 个实例。"""
    from PIL import Image
    im = Image.open(image).convert("RGB")
    im.thumbnail((512, 512))
    W, H = im.size
    px = im.load()
    out = {}

    def masks(prompt):
        ms = []
        for x in sorted(seg.get(prompt) or [], key=lambda x: -x.get("score", 0))[:3]:
            ms.append(Image.open(x["mask"]).convert("L").resize((W, H)).load())
        return ms
    roof_ms = masks("roof")
    for role, prompts in AUTO_ROLES.items():
        prompt = next((q for q in prompts if seg.get(q)), None)   # 候选提示词依次尝试
        excl = roof_ms if role in ("plaster",) else []            # 建筑墙色扣掉屋顶（实测：红屋顶把墙染成红色）
        vals = []
        for mp in masks(prompt) if prompt else []:
            vals += [px[i, j] for j in range(0, H, 2) for i in range(0, W, 2)
                     if mp[i, j] > 127 and not any(r[i, j] > 127 for r in excl)]
        if len(vals) < 12:
            continue
        med = tuple(sorted(v[c] for v in vals)[len(vals) // 2] for c in range(3))
        out[role] = _hex(med)
    return out


def auto_palette(found: dict[str, str], base: str = "citadel") -> dict[str, str]:
    """由区域主色派生完整调色盘（Oskar：语义色角色 + 明度层次），缺的角色取基础调色盘。"""
    pal = dict(PALETTES.get(base, PALETTES["citadel"]))
    black, white = (18, 20, 28), (250, 250, 250)
    if "rock" in found:
        r = _rgb(found["rock"])
        pal.update(rock=_hex(r), rock_alt=_hex(_mix(r, white, 0.14)), rock_dark=_hex(_mix(r, black, 0.4)),
                   rock_top=_hex(_mix(r, white, 0.08)), outline=_hex(_mix(r, black, 0.78)))
    if "sea" in found:
        s = _rgb(found["sea"])
        pal.update(sea=_hex(s), sea_deep=_hex(_mix(s, black, 0.45)), foam=_hex(_mix(s, white, 0.88)),
                   water=_hex(_mix(s, white, 0.72)))
    if "plaster" in found:
        b = _rgb(found["plaster"])
        pal.update(plaster=_hex(b), stone=_hex(_mix(b, black, 0.1)), stone_alt=_hex(_mix(b, white, 0.1)),
                   trim=_hex(_mix(b, black, 0.3)))
    if "roof" in found:
        pal["roof"] = found["roof"]
    if "dome" in found:
        pal["dome"] = found["dome"]
    if "canopy" in found:
        c = _rgb(found["canopy"])
        pal.update(canopy=_hex(c), shrub=_hex(_mix(c, black, 0.08)), cypress=_hex(_mix(c, black, 0.35)))
        pal.setdefault("grass", _hex(_mix(c, white, 0.1)))
    if "grass" in found:
        pal["grass"] = found["grass"]
    if "crystal" in found:
        c = _rgb(found["crystal"])
        pal.update(crystal=_hex(_mix(c, white, 0.2)), crystal_alt=_hex(c))
    return pal


def color_space(corners: list[str], weights: list[float]) -> str:
    """色域取样（x.com/OskSta 2023-10-11 调色板工具）：0D 点 / 1D 渐变 / 2D 三角形，按重心权重插值。"""
    total = sum(weights) or 1.0
    rgb = [0.0, 0.0, 0.0]
    for c, w in zip(corners, weights):
        for i, v in enumerate(_rgb(c)):
            rgb[i] += v * w / total
    return _hex(tuple(round(v) for v in rgb))


def derive(pal: dict[str, str]) -> dict[str, str]:
    """补齐派生角色：地表三阶色斑、屋顶三角色域、岸外波纹。已有的角色不覆盖。"""
    out = dict(pal)
    grass, shrub = out.get("grass", "#6F8C48"), out.get("shrub", "#56793C")
    out.setdefault("grass_alt", color_space([grass, "#E9E2A6"], [0.78, 0.22]))     # 受光的草：偏暖偏亮
    out.setdefault("grass_dark", color_space([grass, shrub, out.get("rock_dark", "#666B6C")], [0.5, 0.38, 0.12]))
    roof, dome, dark = out.get("roof", "#A5654E"), out.get("dome", "#D9B97E"), out.get("rock_dark", "#666B6C")
    out.setdefault("roof_b", color_space([roof, dome, dark], [0.62, 0.3, 0.08]))
    out.setdefault("roof_c", color_space([roof, dome, dark], [0.74, 0.04, 0.22]))
    out.setdefault("ripple", color_space([out.get("foam", "#EEF5F6"), out.get("sea", "#2E98B5")], [0.7, 0.3]))
    return out


def resolve(palette, base: str | None = None) -> dict[str, str]:
    """程序里的 palette 可以是名称或（自动配色得到的）色值字典。"""
    if isinstance(palette, dict):
        return derive({**PALETTES.get(base or "citadel", PALETTES["citadel"]), **palette})
    return derive(PALETTES.get(palette or base or "citadel", PALETTES["citadel"]))
