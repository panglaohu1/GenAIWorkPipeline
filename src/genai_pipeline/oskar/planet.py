"""把多个场景串到同一颗小行星上（参考 x.com/OskSta 2024-03-15“放到球面上”、2024-03-18 球面瓦片山体）。

坐标：Blender 世界坐标，Z 向上；行星中心在原点，海平面半径 R。
每个场景是一块平面 diorama（海平面 z=0），在它的切平面坐标系 (x 东, y 北, z 上) 里用“方位等距投影”贴到球面：
  离场景中心的平面距离 d → 球面上的角距离 d / R，高度 z → 半径 R + z；
因此场景越靠外越往下弯，建筑保持径向直立；地形与网格不需要重建。
场景沿一条略倾斜的大圆“旅程带”依次排开（间距按岛屿大小），高潮场景稍抬高纬度。
相邻两站之间不搭桥：Oskar 读两站相对一侧的岛缘高度，为这段连接选一种地势（阶梯台地 / 山脊垭口 / 低地地峡），
用同一个 Oskar 引擎把这段连接地形生成出来（两端伸进两站岛缘），再按同样的投影贴到球面上——
参考 x.com/OskSta 2022-10 “大地势 / 复杂地势生成”：场景之间是由地势协调出来的，而不是桥。
纯 Python（不依赖 Blender），供 Blender worker 的 compose_planet、Godot 配置与测试共用。
"""
from __future__ import annotations

import math

Vec = tuple[float, float, float]


def _norm(v: Vec) -> Vec:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def unit(lat: float, lon: float) -> Vec:
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))


def frame(lat: float, lon: float, yaw: float = 0.0) -> tuple[Vec, Vec, Vec]:
    """场景中心的单位法向 c 与切平面基 (e 东, n 北)；yaw 让场景绕自身法向转（度，逆时针）。"""
    c = unit(lat, lon)
    lo = math.radians(lon)
    e = (-math.sin(lo), math.cos(lo), 0.0)
    n = _cross(c, e)
    if yaw:
        a = math.radians(yaw)
        e, n = (tuple(math.cos(a) * ei + math.sin(a) * ni for ei, ni in zip(e, n)),
                tuple(-math.sin(a) * ei + math.cos(a) * ni for ei, ni in zip(e, n)))
    return c, e, n


def to_sphere(p: Vec, fr: tuple[Vec, Vec, Vec], radius: float) -> Vec:
    """平面场景里的点 (x, y, z) → 球面世界坐标（方位等距投影；z 为离海平面的高度）。"""
    c, e, n = fr
    x, y, z = p
    d = math.hypot(x, y)
    r = radius + z
    if d < 1e-9:
        return (c[0] * r, c[1] * r, c[2] * r)
    th = d / radius
    ux, uy = x / d, y / d
    u = (e[0] * ux + n[0] * uy, e[1] * ux + n[1] * uy, e[2] * ux + n[2] * uy)
    ct, st = math.cos(th), math.sin(th)
    return ((c[0] * ct + u[0] * st) * r, (c[1] * ct + u[1] * st) * r, (c[2] * ct + u[2] * st) * r)


def island_radius(size: list[float]) -> float:
    """导出尺寸含海盘；岛屿本体大约占海盘的 0.8。"""
    return 0.36 * max(float(size[0]), float(size[1]))


def plan(order: list[str], sizes: dict[str, list[float]], *, climax: str | None = None, gap: float = 6.0,
         spacing: float = 1.0, rotations: dict[str, float] | None = None, nudges: dict[str, list[float]] | None = None,
         tilt: float = 26.0) -> dict:
    """按旅程顺序把场景排在一条倾斜大圆带上。返回 {"radius", "scenes": [{id, lat, lon, yaw, r}], "links": [[a, b], ...]}。
    nudges 单位为米（沿东 / 北），换算成经纬度；rotations 为绕法向的转角（度）。"""
    rotations, nudges = rotations or {}, nudges or {}
    rs = {sid: island_radius(sizes[sid]) for sid in order}
    arcs = [2 * rs[sid] + gap for sid in order]
    circumference = sum(arcs)
    # 半径：既要让环带放得下所有场景，又不能小到把单个场景弯得太厉害（岛缘离中心的角距离不超过约 32°）
    radius = max(circumference / (2 * math.pi), max(rs.values()) * 1.6) * spacing
    scenes, lon = [], 0.0
    for i, sid in enumerate(order):
        if i:
            lon += math.degrees((arcs[i - 1] / 2 + arcs[i] / 2) / radius)
        lat = math.sin(math.radians(lon)) * tilt           # 倾斜的旅程带：起伏的纬度
        if sid == climax:
            lat += 10.0                                    # 高潮抬高纬度：转动星球时它在“上方”出现
        dn = nudges.get(sid) or [0.0, 0.0]
        scenes.append({"id": sid, "r": round(rs[sid], 3),
                       "lat": round(max(-70.0, min(70.0, lat + math.degrees(dn[1] / radius))), 3),
                       "lon": round(lon + math.degrees(dn[0] / radius), 3),
                       "yaw": round(float(rotations.get(sid, 0.0)), 2)})
    # 经度整体居中（第一站与最后一站对称），并把环带的总经度跨度归一到 360° 以内
    span = scenes[-1]["lon"] if scenes else 0.0
    for s in scenes:
        s["lon"] = round(s["lon"] - span / 2, 3)
    links = [[order[i], order[i + 1]] for i in range(len(order) - 1)]
    if len(order) > 2 and span > 250:                      # 绕满一圈时首尾相接，成为环
        links.append([order[-1], order[0]])
    return {"radius": round(radius, 3), "scenes": scenes, "links": links, "gap": gap * spacing}


def slerp(a: Vec, b: Vec, t: float) -> Vec:
    om = math.acos(max(-1.0, min(1.0, _dot(a, b))))
    if om < 1e-6:
        return a
    s = math.sin(om)
    k0, k1 = math.sin((1 - t) * om) / s, math.sin(t * om) / s
    return _norm((a[0] * k0 + b[0] * k1, a[1] * k0 + b[1] * k1, a[2] * k0 + b[2] * k1))


def link_path(a: dict, b: dict, radius: float, step: float = 2.0) -> list[Vec]:
    """两站之间的陆桥中心线（单位向量）：从 A 岛缘出发沿大圆到 B 岛缘，两端各伸进岛内一点以便接上。"""
    ca, cb = unit(a["lat"], a["lon"]), unit(b["lat"], b["lon"])
    om = math.acos(max(-1.0, min(1.0, _dot(ca, cb))))
    if om < 1e-6:
        return []
    t0 = max(0.0, (a["r"] * 0.7) / radius / om)
    t1 = min(1.0, 1 - (b["r"] * 0.7) / radius / om)
    if t1 <= t0:
        return []
    n = max(2, int((t1 - t0) * om * radius / step))
    return [slerp(ca, cb, t0 + (t1 - t0) * i / n) for i in range(n + 1)]


def ribbon(line: list[Vec], radius: float, width: float = 5.0, height: float = 1.2, depth: float = 2.5,
           path_width: float = 1.6) -> dict[str, list]:
    """陆桥网格（按材质角色分组）：顶面草地 + 中间一条石板路 + 两侧崖面（伸到海面以下）。
    返回 {"grass": [quad...], "path": [...], "rock": [...]}，每个 quad 为 4 个世界坐标点（逆时针朝外）。"""
    out = {"grass": [], "path": [], "rock": []}
    if len(line) < 2:
        return out
    hw, pw = width / 2, path_width / 2

    def side(i):
        p = line[i]
        q = line[min(i + 1, len(line) - 1)] if i < len(line) - 1 else line[i]
        o = line[max(i - 1, 0)]
        t = _norm((q[0] - o[0], q[1] - o[1], q[2] - o[2]))
        return _norm(_cross(t, p))

    def at(i, off, z):
        p, s = line[i], side(i)
        ang = off / radius
        d = _norm((p[0] * math.cos(ang) + s[0] * math.sin(ang), p[1] * math.cos(ang) + s[1] * math.sin(ang),
                   p[2] * math.cos(ang) + s[2] * math.sin(ang)))
        r = radius + z
        return (d[0] * r, d[1] * r, d[2] * r)

    # 端头收窄，看起来是从岛上伸出来的岬角
    def w(i):
        k = min(i, len(line) - 1 - i)
        return hw * (0.75 + 0.25 * min(1.0, k / 2))

    for i in range(len(line) - 1):
        a0, a1 = w(i), w(i + 1)
        lt = [at(i, -a0, height), at(i + 1, -a1, height)]
        rt = [at(i, a0, height), at(i + 1, a1, height)]
        pl = [at(i, -pw, height + 0.03), at(i + 1, -pw, height + 0.03)]
        pr = [at(i, pw, height + 0.03), at(i + 1, pw, height + 0.03)]
        out["grass"].append([lt[0], rt[0], rt[1], lt[1]])
        out["path"].append([pl[0], pr[0], pr[1], pl[1]])
        # 两侧崖面：略向外放坡（收分），伸到海面以下
        lb = [at(i, -a0 * 1.25, -depth), at(i + 1, -a1 * 1.25, -depth)]
        rb = [at(i, a0 * 1.25, -depth), at(i + 1, a1 * 1.25, -depth)]
        out["rock"].append([lb[0], lt[0], lt[1], lb[1]])
        out["rock"].append([rt[0], rb[0], rb[1], rt[1]])
    return out


def transform_points(points: list, fr, radius: float) -> list:
    return [[round(c, 3) for c in to_sphere(tuple(p), fr, radius)] for p in points]


def apply_ops(state: dict, ops: list[dict], ids: list[str]) -> tuple[dict, list[str]]:
    """Oskar 整合时只能调排布：间距（改变球半径）、朝向、经纬度微调（米）、相邻交换；
    以及相邻两站之间的地势连接（类型 / 宽度 / 垭口高低），记在 link_overrides 里，下一轮按它重建连接地形。"""
    import copy
    st = copy.deepcopy(state)
    st.setdefault("spacing", 1.0)
    st.setdefault("rotations", {})
    st.setdefault("nudges", {})
    st.setdefault("link_overrides", {})
    log = []
    for op in ops[:4]:
        try:
            k = op.get("op")
            if k == "spacing":
                st["spacing"] = round(max(0.6, min(1.8, st["spacing"] * float(op.get("factor", 1)))), 3)
            elif k == "rotate" and op.get("scene") in ids:
                st["rotations"][op["scene"]] = round((st["rotations"].get(op["scene"], 0) + float(op.get("deg", 0))) % 360, 1)
            elif k == "nudge" and op.get("scene") in ids:
                n = st["nudges"].get(op["scene"], [0, 0])
                st["nudges"][op["scene"]] = [round(max(-20, min(20, n[0] + float(op.get("dx", 0)))), 2),
                                             round(max(-20, min(20, n[1] + float(op.get("dy", 0)))), 2)]
            elif k == "swap" and op.get("a") in ids and op.get("b") in ids:
                o = st["order"]
                i, j = o.index(op["a"]), o.index(op["b"])
                if abs(i - j) != 1:
                    log.append(f"跳过：只交换相邻场景（{op['a']} ↔ {op['b']}）")
                    continue
                o[i], o[j] = o[j], o[i]
            elif k == "link" and op.get("a") in ids and op.get("b") in ids:
                o = st["order"]
                a, b = op["a"], op["b"]
                ring = bool(st.get("ring")) and {o.index(a), o.index(b)} == {0, len(o) - 1}
                if abs(o.index(a) - o.index(b)) != 1 and not ring:
                    log.append(f"跳过：{a} 与 {b} 不相邻，没有连接")
                    continue
                if (o.index(a) > o.index(b)) != ring:
                    a, b = b, a
                ov = dict(st["link_overrides"].get(f"link_{a}__{b}") or {})
                if op.get("kind") in LINK_KINDS:
                    ov["kind"] = op["kind"]
                if op.get("width") is not None:
                    ov["width"] = round(max(7.0, min(18.0, float(op["width"]))), 1)
                if op.get("saddle") is not None:
                    ov["saddle"] = int(max(-2, min(2, round(float(op["saddle"])))))
                st["link_overrides"][f"link_{a}__{b}"] = ov
            else:
                log.append(f"跳过：无效操作 {k}")
                continue
            log.append(str(op))
        except (TypeError, ValueError) as e:
            log.append(f"跳过：{str(e)[:60]}")
    return st, log


def face_rotation(scene: dict, view: Vec = (0.55, -1.5, 0.42), elevation: float = 62.0) -> list[list[float]]:
    """把某个场景转到镜头前（法向朝相机方向再上抬 elevation 度，像平面场景的低机位俯看），返回 3×3 旋转矩阵（行）。"""
    d = _norm(view)
    up = _norm((-d[2] * d[0], -d[2] * d[1], 1 - d[2] * d[2]))
    a = math.radians(elevation)
    c2 = _norm(tuple(math.cos(a) * d[i] + math.sin(a) * up[i] for i in range(3)))
    e2 = _norm(_cross((-d[0], -d[1], -d[2]), (0.0, 0.0, 1.0)))
    k = _dot(e2, c2)
    e2 = _norm(tuple(e2[i] - k * c2[i] for i in range(3)))
    n2 = _cross(c2, e2)
    c, e, n = frame(scene["lat"], scene["lon"], scene.get("yaw", 0.0))
    return [[round(e2[i] * e[j] + n2[i] * n[j] + c2[i] * c[j], 6) for j in range(3)] for i in range(3)]


# ───────────────────────── 地势连接（替代陆桥） ─────────────────────────

LINK_KINDS = {"terraces": "阶梯台地", "ridge": "山脊垭口", "isthmus": "低地地峡"}


def lat_lon(u: Vec) -> tuple[float, float]:
    return math.degrees(math.asin(max(-1.0, min(1.0, u[2])))), math.degrees(math.atan2(u[1], u[0]))


def _toward(src: dict, dst: dict) -> tuple[float, float]:
    """src 场景（含 yaw）的平面坐标里，指向 dst 场景中心的单位方向。"""
    c, e, n = frame(src["lat"], src["lon"], src.get("yaw", 0.0))
    cb = unit(dst["lat"], dst["lon"])
    k = _dot(c, cb)
    t = _norm((cb[0] - k * c[0], cb[1] - k * c[1], cb[2] - k * c[2]))
    ux, uy = _dot(t, e), _dot(t, n)
    d = math.hypot(ux, uy) or 1.0
    return ux / d, uy / d


def edge_profile(walk: list, direction: tuple[float, float], band: float = 7.0) -> dict:
    """岛缘读数：沿 direction 看过去，最外侧可站立地面离场景中心多远（米）、有多高（米，海面为 0）。
    只看靠近连接轴线的一条带子；带子里没有地面时退回整座岛的最外点。"""
    ux, uy = direction
    pts = [(p[0] * ux + p[1] * uy, -p[0] * uy + p[1] * ux, p[2]) for p in walk or [] if len(p) >= 3]
    if not pts:
        return {"edge": 0.0, "z": 2.0, "n": 0}
    near = [p for p in pts if abs(p[1]) <= band] or pts
    edge = max(p[0] for p in near)
    rim = sorted(p[2] for p in near if p[0] >= edge - 6.0)
    return {"edge": round(edge, 2), "z": round(rim[len(rim) // 2], 2), "n": len(rim)}


def link_specs(layout: dict, walks: dict[str, list], *, level_h: float = 2.0, sea_level: int = 1) -> list[dict]:
    """每条相邻连接的几何读数：两站中心角距、双方岛缘离中心距离与高度（换算成层）、岛缘之间的海面间隙，
    以及这段连接地形自己的切平面（中心放在间隙正中，本地 +x 指向 B）。"""
    R = layout["radius"]
    by_id = {s["id"]: s for s in layout["scenes"]}
    out = []
    for a, b in layout.get("links", []):
        if a not in by_id or b not in by_id:
            continue
        A, B = by_id[a], by_id[b]
        ca, cb = unit(A["lat"], A["lon"]), unit(B["lat"], B["lon"])
        om = math.acos(max(-1.0, min(1.0, _dot(ca, cb))))
        D = om * R
        ea = edge_profile(walks.get(a) or [], _toward(A, B))
        eb = edge_profile(walks.get(b) or [], _toward(B, A))
        gap = D - ea["edge"] - eb["edge"]
        mid = slerp(ca, cb, (ea["edge"] + gap / 2) / D) if D > 1e-6 else ca
        lat, lon = lat_lon(mid)
        _, e0, n0 = frame(lat, lon, 0.0)
        k = _dot(mid, cb)
        t = _norm((cb[0] - k * mid[0], cb[1] - k * mid[1], cb[2] - k * mid[2]))
        yaw = math.degrees(math.atan2(_dot(t, n0), _dot(t, e0)))

        def lv(z):
            return int(max(sea_level + 1, min(sea_level + 14, round(z / level_h) + sea_level)))
        out.append({"id": f"link_{a}__{b}", "a": a, "b": b, "lat": round(lat, 4), "lon": round(lon, 4),
                    "yaw": round(yaw, 3), "distance": round(D, 2), "gap": round(gap, 2),
                    "edge_a": ea["edge"], "edge_b": eb["edge"], "z_a": ea["z"], "z_b": eb["z"],
                    "h_a": lv(ea["z"]), "h_b": lv(eb["z"]), "level_h": level_h, "sea_level": sea_level})
    return out


def default_link(spec: dict) -> dict:
    """没有 Oskar 判断时的地势规则：落差大 → 阶梯台地；两端都高 → 山脊垭口；否则低地地峡。"""
    s, ha, hb = spec["sea_level"], spec["h_a"], spec["h_b"]
    if abs(ha - hb) >= 3:
        kind = "terraces"
    elif min(ha, hb) >= s + 3:
        kind = "ridge"
    else:
        kind = "isthmus"
    return {"kind": kind, "width": 11.0, "saddle": 0}


def link_program(spec: dict, plan: dict | None = None, *, palette=None, palette_base: str | None = None,
                 vegetation: str | None = None, seed: int = 0, overlap: float = 7.0) -> dict:
    """把一段连接写成 Oskar 引擎的场景程序（与各站同一种 DSL、同一格网与层高）。
    本地坐标：x 沿 A→B，间隙正中为原点；两端各伸进岛缘 overlap 米，顶面高度对齐岛缘，与岛融成一片。"""
    plan = {**default_link(spec), **(plan or {})}
    kind = plan["kind"] if plan.get("kind") in LINK_KINDS else default_link(spec)["kind"]
    s = int(spec["sea_level"])
    ha, hb = int(spec["h_a"]), int(spec["h_b"])
    width = max(6.0, min(20.0, float(plan.get("width") or 11.0)))
    g = max(4.0, float(spec["gap"]))
    xa, xb = -g / 2, g / 2
    ov = overlap
    ext = max(g / 2 + ov + 1.0, width * 0.75)
    rg = max(4, min(16, math.ceil(ext / (4 * 0.85 * 0.9))))
    S = rg * 4 * 0.85
    rnd = __import__("random").Random(seed * 7907 + ha * 31 + hb)

    def n(v):
        return round(v / S, 4)

    def mass(x, y, length, w, top, rot=0.0, shape="rect", bottom=0):
        return {"type": "mass", "kind": "rock", "at": [n(x), n(y)], "size": [n(length), n(w)], "bottom": bottom,
                "top": int(top), "rot": round(rot, 1), "shape": shape}

    def path(p0, p1, levels, mid_y=0.0):
        pts = [[n(p0), 0.0], [n((p0 + p1) / 2), n(mid_y)], [n(p1), 0.0]]
        return {"type": "path", "points": pts, "levels": [int(levels[0]), int(levels[1])], "width": n(2.3), "clear": 3}

    items = [mass(xa - ov / 2, 0, ov + 2.5, width * 0.9, ha, rnd.uniform(-5, 5)),     # 两端：伸进岛缘的台地，顶面对齐岛缘
             mass(xb + ov / 2, 0, ov + 2.5, width * 0.9, hb, rnd.uniform(-5, 5))]
    saddle = None
    if kind == "terraces":
        steps = max(2, min(6, abs(hb - ha) // 2 + 2))
        seg = (xb - xa) / steps
        for i in range(steps):
            f = (i + 0.5) / steps
            top = round(ha + (hb - ha) * f)
            items.append(mass(xa + seg * (i + 0.5), (1 if i % 2 else -1) * width * 0.08, seg * 1.35,
                              width * (0.95 - 0.1 * (i % 2)), top, rnd.uniform(-6, 6)))
        items.append(path(xa - ov * 0.6, xb + ov * 0.6, (ha, hb), mid_y=width * 0.12))
        for i in range(2):
            f = 0.3 + 0.4 * i
            items.append({"type": "tree", "at": [n(xa + (xb - xa) * f), n((-1 if i else 1) * width * 0.3)], "r": n(1.5)})
    elif kind == "ridge":
        saddle = max(s + 2, min(ha, hb) - 2 + int(plan.get("saddle") or 0))
        saddle = min(saddle, max(ha, hb))
        crest = max(ha, hb)
        items.append(mass(0, 0, g + 3, width * 0.85, saddle, rnd.uniform(-3, 3)))
        items.append(mass(xa + g * 0.18, 0, g * 0.4, width * 0.9, (ha + saddle + 1) // 2))
        items.append(mass(xb - g * 0.18, 0, g * 0.4, width * 0.9, (hb + saddle + 1) // 2))
        for sgn, dx in ((1, -0.12), (-1, 0.14)):   # 垭口两侧的山肩：路从两座峰之间穿过
            items.append({"type": "stack", "at": [n(g * dx), n(sgn * width * 0.62)],
                          "tiers": [{"r": n(width * 0.3), "top": crest + 1}, {"r": n(width * 0.18), "top": crest + 3}]})
        items.append(path(xa - ov * 0.6, 0, (ha, saddle)))
        items.append(path(0, xb + ov * 0.6, (saddle, hb)))
    else:   # isthmus
        low = s + 1
        saddle = low
        items.append(mass(0, 0, g + ov, width * 1.15, low, rnd.uniform(-4, 4), shape="ellipse"))
        for x, h in ((xa + g * 0.12, ha), (xb - g * 0.12, hb)):
            if h > low + 1:
                items.append(mass(x, 0, g * 0.3, width * 0.8, (h + low) // 2))
        items.append({"type": "stack", "at": [n(g * 0.1), n(width * 0.62)], "tiers": [{"r": n(width * 0.16), "top": low + 2}]})
        items.append(path(xa - ov * 0.6, xa + g * 0.3, (ha, low)))
        items.append(path(xa + g * 0.3, xb - g * 0.3, (low, low), mid_y=-width * 0.15))
        items.append(path(xb - g * 0.3, xb + ov * 0.6, (low, hb)))
        items.append({"type": "tree", "at": [n(-g * 0.05), n(-width * 0.32)], "r": n(1.4)})
    prog = {"scene": f"连接 {spec['a']} → {spec['b']}（{LINK_KINDS[kind]}）", "seed": int(seed),
            "grid": {"radius": rg, "cell": 2.0}, "level_h": float(spec["level_h"]), "sea_level": s,
            "scene_type": "other", "rock_style": plan.get("rock_style") or "layered", "items": items}
    if palette is not None:
        prog["palette"] = palette
    if palette_base:
        prog["palette_base"] = palette_base
    if vegetation:
        prog["vegetation"] = vegetation
    prog["_link"] = {"kind": kind, "width": round(width, 2), "saddle": saddle, "h_a": ha, "h_b": hb, "gap": round(g, 2)}
    return prog


def link_frame(spec: dict) -> tuple[Vec, Vec, Vec]:
    return frame(spec["lat"], spec["lon"], spec.get("yaw", 0.0))


def journey(scenes: dict[str, dict], layout: dict, names: dict[str, dict],
            links: list[dict] | None = None) -> tuple[list, list, list]:
    """各场景与各段地势连接的可站立点 / 通路贴到球面（Blender 坐标）；每站给出中心、入口与出口。
    links 为地势连接记录（含 lat/lon/yaw 与引擎给出的 walk/paths）；未给时（旧版陆桥）退回陆桥中心线。"""
    R = layout["radius"]
    by_id = {s["id"]: s for s in layout["scenes"]}
    walk, paths, stops = [], [], []
    for s in layout["scenes"]:
        rec = scenes[s["id"]]
        fr = frame(s["lat"], s["lon"], s.get("yaw", 0.0))
        w = transform_points(rec.get("walk") or [], fr, R)
        ps = [transform_points(p, fr, R) for p in rec.get("paths") or [] if p]
        walk += w
        paths += ps
        center = [round(c, 3) for c in to_sphere((0.0, 0.0, 0.0), fr, R)]
        entry = ps[0][0] if ps else (w[0] if w else center)
        exit_ = ps[-1][-1] if ps else (max(w, key=lambda p: math.dist(p, entry)) if w else center)
        meta = names.get(s["id"], {})
        stops.append({"id": s["id"], "name": meta.get("name", s["id"]), "role": meta.get("role"),
                      "link_to_next": meta.get("link_to_next"), "center": center, "entry": entry, "exit": exit_,
                      "lat": s["lat"], "lon": s["lon"]})
    if links is not None:
        for lk in links:
            fr = link_frame(lk)
            walk += transform_points(lk.get("walk") or [], fr, R)
            paths += [transform_points(p, fr, R) for p in lk.get("paths") or [] if p]
        return walk, paths, stops
    for a, b in layout.get("links", []):
        if a in by_id and b in by_id:
            line = link_path(by_id[a], by_id[b], R)
            if line:
                paths.append([[round(v * (R + 1.25), 3) for v in p] for p in line])
    return walk, paths, stops


# ───────────────────────── 星球玩法：每站一封信 + 环球有轨电车 ─────────────────────────

def reachable(walk: list, start: tuple[float, float], *, hop: float = 2.6, climb: float = 2.3) -> list:
    """从离 start（平面坐标）最近的可站立点出发，沿“水平相距 ≤ hop、高差 ≤ climb”的相邻关系能走到的点
    （小老虎能自动攀上 ≤ 2.3 米的台阶；海中孤立的岩柱顶走不到）。"""
    pts = [p for p in walk or [] if len(p) >= 3]
    if not pts:
        return []
    s0 = min(range(len(pts)), key=lambda i: (pts[i][0] - start[0]) ** 2 + (pts[i][1] - start[1]) ** 2 + 0.5 * pts[i][2] ** 2)
    seen, stack = {s0}, [s0]
    h2 = hop * hop
    while stack:
        i = stack.pop()
        p = pts[i]
        for j, q in enumerate(pts):
            if j not in seen and (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 <= h2 and abs(q[2] - p[2]) <= climb:
                seen.add(j)
                stack.append(j)
    return [pts[i] for i in sorted(seen)]


def _letter_spot(walk: list, toward_entry: tuple[float, float] | None, edge: float = 12.0,
                 paths: list | None = None) -> list | None:
    """场景里放信的位置（平面坐标）：从来路一侧的岛缘能走到（台阶 ≤ 2.3 米可自动攀上），在“台地”上
    （周围 2.5 米内有 ≥4 个可站立点，不是单格屋顶），偏高、离来路远——让小老虎去探险。"""
    ux, uy = toward_entry or (0.0, -1.0)
    # 通路（台阶 / 坡道）上的点也算可走：坡道格不在 walk 里，不加它们台地之间就断开了
    net = reachable(list(walk or []) + [list(q) for path in paths or [] for q in path], (ux * edge, uy * edge), hop=2.8)
    if len(net) < 0.3 * len(walk or []):   # 来路一侧落在孤立的小块上（码头、岩柱）：换成最大的连通台地
        allp = list(walk or []) + [list(q) for path in paths or [] for q in path]
        left, best = [tuple(p) for p in allp], []
        while left and len(best) < len(left):
            comp = reachable([list(p) for p in left], left[0][:2], hop=2.8)
            if len(comp) > len(best):
                best = comp
            cs = {tuple(p) for p in comp}
            left = [p for p in left if p not in cs]
        net = best or net
    on_walk = {tuple(p) for p in walk or []}
    pts = [p for p in net if tuple(p) in on_walk] or [p for p in walk or [] if len(p) >= 3]
    if not pts:
        return None
    zmax = max(p[2] for p in pts) or 1.0

    def dense(p):
        return sum(1 for q in pts if abs(q[2] - p[2]) < 0.3 and (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 < 6.25) >= 4
    cands = [p for p in pts if dense(p)] or pts
    ext = max(math.hypot(p[0], p[1]) for p in cands) or 1.0
    return max(cands, key=lambda p: 0.55 * p[2] / zmax - 0.45 * (p[0] * ux + p[1] * uy) / ext)


def game_layout(scenes: dict[str, dict], layout: dict, names: dict[str, dict], *, ring: float = 1.12,
                step: float = 2.0) -> dict:
    """玩法布置（Blender 坐标）：
    letters —— 每站一封信，放在偏高的台地上（[{scene, name, pos}]）；
    tram —— 环球有轨电车线路：在每站自己的平面里沿岛缘外侧、贴着海岸的弧（Oskar 式连拱堤桥：从朝上一站的方向
             绕到朝下一站的方向），站与站之间沿大圆走过地势连接，末站绕回首站成环。只给路线的方向（单位向量），
             轨道高度在 Godot 里按地表射线逐点求（海上贴着水面 1.6 米，遇到连接地势就按限坡爬过去）；
             segments 标出每站的弧段，用来设站。
             2026-10-07：原来走岛缘内侧（ring 0.9），轨道压在崖顶上，中位高度离地 6 米、最高 27 米，像过山车。"""
    R = layout["radius"]
    order = [s["id"] for s in layout["scenes"]]
    by_id = {s["id"]: s for s in layout["scenes"]}
    letters, arcs = [], []
    n = len(order)
    for i, sid in enumerate(order):
        s = by_id[sid]
        fr = frame(s["lat"], s["lon"], s.get("yaw", 0.0))
        walk = (scenes.get(sid) or {}).get("walk") or []
        prev = by_id[order[i - 1]] if n > 1 else None
        nxt = by_id[order[(i + 1) % n]] if n > 1 else None
        a_in = _toward(s, prev) if prev else (-1.0, 0.0)
        a_out = _toward(s, nxt) if nxt else (1.0, 0.0)
        spot = _letter_spot(walk, a_in, edge_profile(walk, a_in)["edge"] or s["r"] * 0.7,
                            (scenes.get(sid) or {}).get("paths") or [])
        if spot:
            letters.append({"scene": sid, "name": (names.get(sid) or {}).get("name", sid),
                            "pos": [round(c, 3) for c in to_sphere((spot[0], spot[1], spot[2] + 0.9), fr, R)]})
        e_in = edge_profile(walk, a_in)["edge"] or s["r"] * 0.7
        e_out = edge_profile(walk, a_out)["edge"] or s["r"] * 0.7
        rr = max(6.0, ring * (e_in + e_out) / 2)
        t0, t1 = math.atan2(a_in[1], a_in[0]), math.atan2(a_out[1], a_out[0])
        d = (t1 - t0 + math.pi) % (2 * math.pi) - math.pi
        if abs(d) < 0.6:                       # 进出方向几乎相同（首末站）：绕半圈，走过岛的另一侧
            d = math.pi if d >= 0 else -math.pi
        m = max(4, int(abs(d) * rr / step))
        pts = [(math.cos(t0 + d * k / m) * rr, math.sin(t0 + d * k / m) * rr, 0.0) for k in range(m + 1)]
        arcs.append([_norm(to_sphere(p, fr, R)) for p in pts])
    route, segments = [], []
    for i, arc in enumerate(arcs):
        start = len(route)
        route += arc
        segments.append({"scene": order[i], "name": (names.get(order[i]) or {}).get("name", order[i]),
                         "from": start, "to": len(route) - 1})
        a, b = arc[-1], arcs[(i + 1) % n][0]
        om = math.acos(max(-1.0, min(1.0, _dot(a, b))))
        k = max(1, int(om * R / step))
        route += [slerp(a, b, j / k) for j in range(1, k)]
    # 空气透视：远处（星球另一侧、俯瞰时的远景）向天色淡出，近处（跟拍距离）几乎不受影响；随星球半径缩放
    return {"letters": letters, "fog_density": round(0.1 / max(R, 1.0), 5), "tram": {"route": [[round(c, 5) for c in u] for u in route], "segments": segments,
                                          "radius": R, "loop": True,
                                          "clearance": 1.6, "ground_q": 0.3, "median_window": 8}}
