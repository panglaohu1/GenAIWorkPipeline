extends "res://addons/pipeline/oskar_diorama.gd"
## 星球玩法（多场景世界整合成小行星后）：配置在 res://oskar_scene.json 的 "game"（由流水线 atlas_godot 生成）。
##  · 小老虎在星球上探险（重力指向球心，WASD 移动、空格跳、鼠标拖动转相机），每一站藏着一封信，收齐为止；
##  · 环球有轨电车：轨道沿每站岛缘内侧的弧、经地势连接绕星球一圈，高度按地表射线逐点求（过海处是高架）；
##    电车在各站停靠。小老虎靠近电车时按 F 上车；车上按 C 切换司机主视角（看不到车身，没有遮挡），再按 F 下车。
## 展示巡游（流水线截图核对）时玩法物件隐藏，保证与 Blender 渲染同机位比较。

const PlanetPlayer = preload("res://addons/pipeline/planet_player.gd")
const TRAM_LAYER := 2           # 电车车身单独一层：司机视角的相机不渲染这一层 → 没有任何车身遮挡

var game := {}
var game_root: Node3D
var player: CharacterBody3D
var letters: Array = []          # [{node, scene, name}]
var got := 0
var total := 0
var hud: Label
var _hint_t := 0.0
var tip: Label
var banner: Label
var _banner_t := 0.0

# 轨道
var rail_pts: Array = []         # Vector3（轨面中心）
var rail_up: Array = []          # Vector3（该点的“上”）
var rail_s: Array = []           # 累计长度（米）
var rail_len := 0.0
var stations: Array = []         # [{s, name, scene}]
# 电车
var tram: Node3D
var tram_s := 0.0
var tram_speed := 7.0
var _dwell := 0.0
var _next_station := 0
var riding := false
var driver_view := false
var tram_cam: Camera3D
var driver_cam: Camera3D
var _demo := false
var _demo_t := 0.0
var _game_ready := false
var _tram_drag := false          # 车上拖动鼠标转视角
var _tram_yaw := 0.0
var _tram_pitch := 0.55
var _tram_dist := 11.0
var _called := -1                # 按 T 叫车：电车开往的站（到站后多等一会儿）
var _rail_h_stats := []
var _tst: SurfaceTool
var _rail_tunnel := {}           # 在隧道里的轨道点下标


func _ready() -> void:
	super()
	game = cfg.get("game", {})
	if game.is_empty() or not _planet:
		return
	for a in OS.get_cmdline_user_args():
		if String(a) == "--planet-demo":
			_demo = true
	if _demo:
		_showcase = false
	game_root = Node3D.new()
	game_root.name = "planet_game"
	add_child(game_root)
	await get_tree().physics_frame   # 地形碰撞体要过一帧物理才进空间，之后才能用射线量地表
	await get_tree().physics_frame
	_build_rail()
	_build_tram()
	_build_letters()
	_build_player()
	_build_hud()
	if _showcase:
		game_root.visible = false
		player.visible = false
		player.frozen = true
		for l in [hud, tip, banner]:
			l.visible = false
	else:
		_orbit = false
		hint.visible = false
		player.camera.current = true
	_game_ready = true
	print("PLANET_GAME " + JSON.stringify({"letters": total, "rail_m": snappedf(rail_len, 0.1), "rail_h": _rail_h_stats,
		"stations": stations.map(func(s): return s["name"]), "rail_points": rail_pts.size()}))


func _v(a) -> Vector3:
	return Vector3(float(a[0]), float(a[1]), float(a[2]))


# ───────────────────────── 轨道：按地表逐点求高度 ─────────────────────────

func _surface(dir: Vector3) -> float:
	var space := get_world_3d().direct_space_state
	var q := PhysicsRayQueryParameters3D.create(_planet_c + dir * (_planet_r + 150.0), _planet_c + dir * (_planet_r - 3.0))
	q.collision_mask = 1   # 只量地形，不算树木 / 道具（第 3 层）
	var hit := space.intersect_ray(q)
	return ((hit["position"] as Vector3) - _planet_c).length() if hit else _planet_r


## 轨道是贴海的堤桥：路线点落在岛上（离海面高于 0.8 米）就沿“离开该岛中心”的方向往外推，直到落回海面；
## 推不出去的（岛与岛之间的地势连接）保留，由限坡爬过去。推完再平滑两遍，免得折来折去。
func _avoid_land(dirs: Array) -> Array:
	var centers: Array = []
	for st in cfg.get("journey", []):
		if st.has("center"):
			centers.append((_v(st["center"]) - _planet_c).normalized())
	if centers.is_empty():
		return dirs
	var out: Array = []
	var moved: Array = []
	for d0 in dirs:
		var d: Vector3 = d0
		var mv := false
		if _surface(d) > _planet_r + 0.8:
			var c: Vector3 = centers[0]
			for cc in centers:
				if (cc as Vector3).dot(d) > c.dot(d):
					c = cc
			var away := (d - c * c.dot(d))
			if away.length() > 0.0001:
				away = away.normalized()
				for k in range(1, 16):
					var d2 := (d + away * (2.0 * k / _planet_r)).normalized()
					if _surface(d2) <= _planet_r + 0.6:
						d = d2
						mv = true
						break
		out.append(d)
		moved.append(mv)
	var n := out.size()
	var land_before := 0
	var land_after := 0
	for i in n:
		if _surface(dirs[i]) > _planet_r + 0.8:
			land_before += 1
		if _surface(out[i]) > _planet_r + 0.8:
			land_after += 1
	print("RAIL_LAND " + JSON.stringify({"before": land_before, "after": land_after, "points": n}))
	for _k in 3:
		var sm: Array = []
		for i in n:
			var a: Vector3 = out[(i - 1 + n) % n]
			var b: Vector3 = out[(i + 1) % n]
			sm.append(((out[i] as Vector3) * 2.0 + a + b).normalized())
		out = sm
	return out


func _build_rail() -> void:
	var tr: Dictionary = game.get("tram", {})
	var dirs: Array = []
	for u in tr.get("route", []):
		dirs.append(_v(u).normalized())
	if dirs.size() < 4:
		return
	dirs = _avoid_land(dirs)
	var n := dirs.size()
	var ground: Array = []
	for i in n:
		var d: Vector3 = dirs[i]
		var t: Vector3 = (dirs[(i + 1) % n] - dirs[(i - 1 + n) % n]).normalized()
		var side := t.cross(d).normalized()
		var g := _planet_r
		for off in [-1.8, 0.0, 1.8]:   # 轨道两侧也要让开地面（2.6 米宽的道床）
			g = maxf(g, _surface((d * _planet_r + side * off).normalized()))
		ground.append(g)
	var clear := float(tr.get("clearance", 0.9))
	var step := 0.0
	for i in n:
		step += (dirs[i] as Vector3).distance_to(dirs[(i + 1) % n]) * _planet_r
	step /= n
	var grade := float(tr.get("grade", 0.16)) * step
	# 高度：地表的低分位 + 净空（路线已推到海上；剩下穿过岛间地势连接、窄岩柱的地方走隧道，
	# 隧道里有内壁，司机视角看到的是隧道而不是岩体内部），再平均、再限坡（只抬高不压低）
	var r: Array = []
	var win := int(tr.get("median_window", 6))
	var gq := clampf(float(tr.get("ground_q", 0.5)), 0.0, 1.0)   # 取窗口内地表高度的哪个分位（低分位：窄峰从下面穿过）
	for i in n:
		var vals: Array = []
		for j in range(-win, win + 1):
			vals.append(float(ground[(i + j + n) % n]))
		vals.sort()
		r.append(maxf(float(vals[int(float(vals.size() - 1) * gq)]), _planet_r) + clear)
	for _k in 2:
		var sm: Array = []
		for i in n:
			var acc := 0.0
			for j in range(-2, 3):
				acc += float(r[(i + j + n) % n])
			sm.append(acc / 5.0)
		r = sm
	for _k in 3:
		for i in n:
			r[i] = maxf(float(r[i]), float(r[(i - 1 + n) % n]) - grade)
		for i in range(n - 1, -1, -1):
			r[i] = maxf(float(r[i]), float(r[(i + 1) % n]) - grade)
	var acc_s := 0.0
	for i in n:
		var p: Vector3 = _planet_c + (dirs[i] as Vector3) * float(r[i])
		if i > 0:
			acc_s += p.distance_to(rail_pts[i - 1])
		rail_pts.append(p)
		rail_up.append(dirs[i])
		rail_s.append(acc_s)
	rail_len = acc_s + (rail_pts[n - 1] as Vector3).distance_to(rail_pts[0])
	# 站：每站弧段里轨面离地最近的点（上下车方便）
	for seg in tr.get("segments", []):
		var best := int(seg["from"])
		var bd := INF
		for i in range(int(seg["from"]), int(seg["to"]) + 1):
			var gap := float(r[i]) - float(ground[i])
			if gap < bd - 0.05:
				bd = gap
				best = i
		stations.append({"s": float(rail_s[best]), "i": best, "name": String(seg.get("name", "")),
			"scene": String(seg.get("scene", ""))})
	var hs: Array = []
	for i in n:
		hs.append(snappedf(float(r[i]) - _planet_r, 0.1))
	var gs: Array = []
	for i in n:
		gs.append(float(r[i]) - float(ground[i]))
	gs.sort()
	_rail_h_stats = [hs.min(), hs.max(), snappedf(gs[0], 0.1), snappedf(gs[gs.size() / 2], 0.1), snappedf(gs[-1], 0.1)]
	_rail_mesh(ground, r)
	_build_platforms()


func _rail_mesh(ground: Array, r: Array) -> void:
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	st.set_smooth_group(-1)   # 平面着色
	_tst = SurfaceTool.new()   # 隧道内壁单独一个网格：只朝里（从外面看不见，不会在岸边冒出灰盒子）
	_tst.begin(Mesh.PRIMITIVE_TRIANGLES)
	var n := rail_pts.size()
	var stone := Color(0.84, 0.79, 0.69)
	var stone_dark := Color(0.66, 0.6, 0.52)
	var steel := Color(0.25, 0.25, 0.28)
	var wood := Color(0.45, 0.32, 0.22)
	var tunnel := Color(0.36, 0.32, 0.29)
	var tunnel_top := Color(0.26, 0.23, 0.21)
	var next_pier := 0.0
	var next_sleeper := 0.0
	for i in n:
		var a: Vector3 = rail_pts[i]
		var b: Vector3 = rail_pts[(i + 1) % n]
		var ua: Vector3 = rail_up[i]
		var ub: Vector3 = rail_up[(i + 1) % n]
		var t := (b - a).normalized()
		var sa := t.cross(ua).normalized()
		var sb := t.cross(ub).normalized()
		# 道床：2.6 米宽、0.45 米厚的石砌桥面（Oskar 式米色石，侧面略暗）
		_slab(st, a - ua * 0.12, b - ub * 0.12, sa, sb, ua, ub, 1.3, 0.45, stone, stone_dark)
		# 两条钢轨
		for off in [-0.72, 0.72]:
			_slab(st, a + sa * off + ua * 0.08, b + sb * off + ub * 0.08, sa, sb, ua, ub, 0.06, 0.1, steel, steel)
		# 隧道：这一段轨面低于地表（穿过岛间的地势连接 / 岩柱），加内壁和拱顶（双面），并在洞口加门框
		var inside_a := float(ground[i]) > float(r[i]) + 0.3
		var inside_b := float(ground[(i + 1) % n]) > float(r[(i + 1) % n]) + 0.3
		if inside_a:
			_rail_tunnel[i] = true
		if inside_a or inside_b:
			for sd in [-1.0, 1.0]:
				_tunnel_quad(a + sa * 1.7 * sd - ua * 0.6, b + sb * 1.7 * sd - ub * 0.6, b + sb * 1.7 * sd + ub * 3.6, a + sa * 1.7 * sd + ua * 3.6, -sa * sd, tunnel)
			_tunnel_quad(a - sa * 1.7 + ua * 3.6, b - sb * 1.7 + ub * 3.6, b + sb * 1.7 + ub * 3.6, a + sa * 1.7 + ua * 3.6, -ua, tunnel_top)
			if inside_a != inside_b:
				var pp := a if inside_b else b
				var uu := ua if inside_b else ub
				var ss := sa if inside_b else sb
				for sd in [-1.0, 1.0]:
					_box_oriented(st, pp + ss * 2.0 * sd + uu * 1.5, ss, uu, t, Vector3(0.7, 4.2, 0.7), stone_dark)
				_box_oriented(st, pp + uu * 3.9, ss, uu, t, Vector3(4.7, 0.7, 0.7), stone_dark)
		# 枕木
		var seg_len := a.distance_to(b)
		var s0 := float(rail_s[i])
		while next_sleeper < s0 + seg_len:
			var f := clampf((next_sleeper - s0) / maxf(seg_len, 0.001), 0.0, 1.0)
			var p := a.lerp(b, f)
			var u := ua.slerp(ub, f)
			var sd := sa.slerp(sb, f)
			_box_oriented(st, p + u * 0.02, sd, u, t, Vector3(2.0, 0.08, 0.22), wood)
			next_sleeper += 0.9
		# 桥墩：离地高于 1.6 米处每 9 米一根，落到地面
		while next_pier < s0 + seg_len:
			var f2 := clampf((next_pier - s0) / maxf(seg_len, 0.001), 0.0, 1.0)
			var p2 := a.lerp(b, f2)
			var u2 := ua.slerp(ub, f2)
			var g := lerpf(float(ground[i]), float(ground[(i + 1) % n]), f2)
			var hgt := (p2 - _planet_c).length() - 0.57 - g
			if hgt > 1.6:
				var mid := p2 - u2 * (0.57 + hgt / 2.0 - 0.3)
				_box_oriented(st, mid, sa, u2, t, Vector3(1.1, hgt + 0.6, 0.9), stone_dark)
			next_pier += 9.0
	st.generate_normals()
	var mesh := st.commit()
	var mat := StandardMaterial3D.new()
	mat.vertex_color_use_as_albedo = true
	mat.roughness = 0.9
	if _rail_tunnel.size() > 0:
		var tm := MeshInstance3D.new()
		tm.name = "tram_tunnel"
		tm.mesh = _tst.commit()
		var tmat := StandardMaterial3D.new()
		tmat.vertex_color_use_as_albedo = true
		tmat.roughness = 1.0
		tm.material_override = tmat
		game_root.add_child(tm)
	var mi := MeshInstance3D.new()
	mi.name = "tram_rail"
	mi.mesh = mesh
	mi.material_override = mat
	game_root.add_child(mi)
	var body := StaticBody3D.new()   # 道床可以走上去（小老虎沿高架走也行）
	var cs := CollisionShape3D.new()
	cs.shape = mesh.create_trimesh_shape()
	body.add_child(cs)
	mi.add_child(body)


func _quad(st: SurfaceTool, p0: Vector3, p1: Vector3, p2: Vector3, p3: Vector3, c: Color) -> void:
	st.set_color(c)
	for p in [p0, p1, p2, p0, p2, p3]:
		st.add_vertex(p)


## 隧道内壁：法线朝 inward，绕序按 Godot 的“顺时针为正面”取，只从隧道里面看得见
func _tunnel_quad(p0: Vector3, p1: Vector3, p2: Vector3, p3: Vector3, inward: Vector3, c: Color) -> void:
	var pts := [p0, p1, p2, p0, p2, p3]
	if (p1 - p0).cross(p2 - p0).dot(inward) > 0.0:
		pts = [p0, p2, p1, p0, p3, p2]
	_tst.set_color(c)
	_tst.set_normal(inward.normalized())
	for p in pts:
		_tst.add_vertex(p)


func _slab(st: SurfaceTool, a: Vector3, b: Vector3, sa: Vector3, sb: Vector3, ua: Vector3, ub: Vector3,
		hw: float, th: float, top: Color, side: Color) -> void:
	var al := a - sa * hw
	var ar := a + sa * hw
	var bl := b - sb * hw
	var br := b + sb * hw
	_quad(st, al, bl, br, ar, top)
	_quad(st, ar, br, br - ub * th, ar - ua * th, side)
	_quad(st, bl, al, al - ua * th, bl - ub * th, side)
	_quad(st, ar - ua * th, br - ub * th, bl - ub * th, al - ua * th, side)


func _box_oriented(st: SurfaceTool, c: Vector3, x: Vector3, y: Vector3, z: Vector3, size: Vector3, col: Color) -> void:
	var hx := x * size.x / 2.0
	var hy := y * size.y / 2.0
	var hz := z * size.z / 2.0
	var v := func(sx, sy, sz): return c + hx * sx + hy * sy + hz * sz
	_quad(st, v.call(-1, 1, -1), v.call(-1, 1, 1), v.call(1, 1, 1), v.call(1, 1, -1), col)
	_quad(st, v.call(-1, -1, -1), v.call(1, -1, -1), v.call(1, -1, 1), v.call(-1, -1, 1), col)
	_quad(st, v.call(1, -1, -1), v.call(1, 1, -1), v.call(1, 1, 1), v.call(1, -1, 1), col)
	_quad(st, v.call(-1, -1, 1), v.call(-1, 1, 1), v.call(-1, 1, -1), v.call(-1, -1, -1), col)
	_quad(st, v.call(-1, -1, 1), v.call(1, -1, 1), v.call(1, 1, 1), v.call(-1, 1, 1), col)
	_quad(st, v.call(1, -1, -1), v.call(-1, -1, -1), v.call(-1, 1, -1), v.call(1, 1, -1), col)


## 每站一座栈桥：从轨道旁的站台斜着接到岛上最近、最好走的可站立点，小老虎走上去就能等车
func _build_platforms() -> void:
	if walk_points.is_empty():
		return
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	var plank := Color(0.62, 0.45, 0.3)
	var plank_dark := Color(0.45, 0.32, 0.22)
	for S in stations:
		var i := int(S["i"])
		var p: Vector3 = rail_pts[i]
		var up: Vector3 = rail_up[i]
		var best := Vector3.ZERO
		var bs := INF
		for q in walk_points:
			var v: Vector3 = (q as Vector3) - p
			var hgt := v.dot(up)
			var flat := (v - up * hgt).length()
			if flat > 30.0:
				continue
			var score := flat + 2.5 * absf(hgt)
			if score < bs:
				bs = score
				best = q
		if bs == INF:
			continue
		var flat_dir := (best - p) - up * (best - p).dot(up)
		if flat_dir.length() < 0.01:
			continue
		flat_dir = flat_dir.normalized()
		var a := p + flat_dir * 1.9 - up * 0.1     # 站台边（轨道旁）
		var b := best + up * 0.05
		var t := (b - a).normalized()
		var side := t.cross(up).normalized()
		_slab(st, a, b, side, side, up, up, 0.9, 0.3, plank, plank_dark)                                  # 栈桥
		_slab(st, a - flat_dir * 0.4, a + flat_dir * 0.6, side, side, up, up, 3.0, 0.3, plank, plank_dark)   # 站台
		S["platform"] = a
		S["shore"] = best
		_box_oriented(st, a + up * 1.6 - side * 1.2, side, up, t, Vector3(0.12, 3.2, 0.12), plank_dark)   # 站牌杆
		var sign := Label3D.new()
		sign.text = String(S["name"]) + "站"
		sign.font_size = 96
		sign.pixel_size = 0.012
		sign.billboard = BaseMaterial3D.BILLBOARD_ENABLED
		sign.modulate = Color(1, 0.96, 0.86)
		sign.outline_size = 18
		game_root.add_child(sign)
		sign.global_position = a + up * 3.6
		var lamp_at := a + up * 3.1 - side * 1.2
		_box_oriented(st, lamp_at, side, up, t, Vector3(0.36, 0.36, 0.36), Color(1.0, 0.86, 0.55))   # 站灯（夜里亮）
		add_lantern(lamp_at - Vector3(0, 1.4, 0), null, 2.4, 10.0)   # 夜里站台有一盏暖灯：海上远远就能看到车站
	st.generate_normals()
	var mesh := st.commit()
	var mat := StandardMaterial3D.new()
	mat.vertex_color_use_as_albedo = true
	mat.roughness = 0.9
	var mi := MeshInstance3D.new()
	mi.name = "tram_platforms"
	mi.mesh = mesh
	mi.material_override = mat
	game_root.add_child(mi)
	var body := StaticBody3D.new()
	var cs := CollisionShape3D.new()
	cs.shape = mesh.create_trimesh_shape()
	body.add_child(cs)
	mi.add_child(body)


func _nearest_station(pos: Vector3) -> int:
	var bi := -1
	var bd := INF
	for k in stations.size():
		var q: Vector3 = stations[k].get("platform", rail_pts[int(stations[k]["i"])])
		var d := q.distance_to(pos)
		if d < bd:
			bd = d
			bi = k
	return bi


## T：叫车——电车加速开往离小老虎最近的站，到站后等 30 秒
func call_tram() -> void:
	var k := _nearest_station(player.global_position)
	if k < 0 or tram == null:
		return
	_called = k
	_next_station = k
	if _dwell > 0.0 and absf(tram_s - float(stations[k]["s"])) < 1.0:
		_dwell = 30.0
		_called = -1
	else:
		_dwell = 0.0
	_say("电车正开往「%s站」，到栈桥上的站台等车，车到了按 F 上车" % String(stations[k]["name"]))


func _can_board() -> bool:
	if tram == null or riding:
		return false
	if player.global_position.distance_to(tram.global_position) < 8.0:
		return true
	if _dwell > 0.0:   # 车正停在站上：站在这一站的站台 / 栈桥上都能上
		var k := _nearest_station(tram.global_position)
		if k >= 0 and stations[k].has("platform"):
			var a: Vector3 = stations[k]["platform"]
			var b: Vector3 = stations[k]["shore"]
			var p := player.global_position
			var t := clampf((p - a).dot(b - a) / maxf((b - a).length_squared(), 0.01), 0.0, 1.0)
			return p.distance_to(a.lerp(b, t)) < 3.0
	return false


func rail_at(s: float) -> Dictionary:
	s = fposmod(s, rail_len)
	var n := rail_pts.size()
	var lo := 0
	var hi := n - 1
	while lo < hi:   # 二分：最后一个 rail_s ≤ s 的点
		var mid := (lo + hi + 1) / 2
		if float(rail_s[mid]) <= s:
			lo = mid
		else:
			hi = mid - 1
	var a: Vector3 = rail_pts[lo]
	var b: Vector3 = rail_pts[(lo + 1) % n]
	var seg := a.distance_to(b)
	var f := clampf((s - float(rail_s[lo])) / maxf(seg, 0.001), 0.0, 1.0)
	var up: Vector3 = (rail_up[lo] as Vector3).slerp(rail_up[(lo + 1) % n], f).normalized()
	var fwd := (b - a).normalized()
	return {"pos": a.lerp(b, f), "up": up, "fwd": (fwd - up * fwd.dot(up)).normalized()}


# ───────────────────────── 电车 ─────────────────────────

func _build_tram() -> void:
	if rail_pts.is_empty():
		return
	tram = Node3D.new()
	tram.name = "tram"
	game_root.add_child(tram)
	var cream := Color(0.95, 0.91, 0.82)
	var red := Color(0.78, 0.29, 0.2)
	var glass := Color(0.2, 0.27, 0.34)
	var dark := Color(0.18, 0.17, 0.17)
	var roof := Color(0.72, 0.7, 0.66)
	# 车身（-Z 朝前）：长 7 米、宽 2.2 米；地板在轨面上 0.55 米
	_tram_box(Vector3(0, 0.95, 0), Vector3(2.2, 0.8, 7.0), cream)           # 下半车身
	_tram_box(Vector3(0, 1.38, 0), Vector3(2.22, 0.12, 7.02), red)          # 腰线
	_tram_box(Vector3(0, 1.85, 0), Vector3(2.16, 0.82, 6.8), glass)         # 车窗带
	for z in [-3.0, -1.5, 0.0, 1.5, 3.0]:                                   # 窗柱
		_tram_box(Vector3(0, 1.85, z), Vector3(2.2, 0.82, 0.14), cream)
	_tram_box(Vector3(0, 2.35, 0), Vector3(2.24, 0.2, 7.04), red)           # 檐口
	_tram_box(Vector3(0, 2.5, 0), Vector3(1.9, 0.12, 6.6), roof)            # 车顶
	_tram_box(Vector3(0, 0.38, -2.4), Vector3(1.6, 0.36, 1.6), dark)        # 转向架
	_tram_box(Vector3(0, 0.38, 2.4), Vector3(1.6, 0.36, 1.6), dark)
	_tram_box(Vector3(0, 1.1, -3.52), Vector3(0.5, 0.18, 0.04), Color(1.0, 0.93, 0.6))   # 前灯
	_tram_box(Vector3(0, 2.9, 0.3), Vector3(0.06, 0.7, 0.06), dark)         # 受电弓
	_tram_box(Vector3(0, 3.25, 0.3), Vector3(1.2, 0.05, 0.08), dark)
	tram_cam = Camera3D.new()
	tram_cam.fov = 62.0
	tram_cam.near = 0.1
	tram.add_child(tram_cam)
	tram_cam.position = Vector3(0, 6.5, 10.0)
	tram_cam.rotation = Vector3(deg_to_rad(-30), 0, 0)
	call_deferred("_place_tram_cam")
	driver_cam = Camera3D.new()
	driver_cam.fov = 75.0
	driver_cam.near = 0.05
	driver_cam.cull_mask = 0xFFFFF & ~(1 << (TRAM_LAYER - 1))   # 不渲染车身层：司机视角无遮挡
	tram.add_child(driver_cam)
	driver_cam.position = Vector3(0, 1.9, -3.2)
	driver_cam.rotation = Vector3(deg_to_rad(-14), 0, 0)   # 小星球曲率大：略低头才看得到前方的轨道与场景
	tram_s = float(stations[0]["s"]) if not stations.is_empty() else 0.0
	_dwell = 3.0
	_next_station = 1 % maxi(stations.size(), 1)
	_tram_lights()
	_place_tram()


func _tram_box(pos: Vector3, size: Vector3, c: Color) -> void:
	var mi := MeshInstance3D.new()
	var bm := BoxMesh.new()
	bm.size = size
	mi.mesh = bm
	var m := StandardMaterial3D.new()
	m.albedo_color = c
	m.roughness = 0.6
	mi.material_override = m
	mi.position = pos
	mi.layers = 1 << (TRAM_LAYER - 1)
	tram.add_child(mi)


func _tram_lights() -> void:
	# 车头灯：夜里电车沿环线跑时是一团移动的暖光
	if tram == null:
		return
	add_lantern(Vector3.ZERO, tram, 2.0, 9.0)


func _place_tram() -> void:
	var a := rail_at(tram_s - 3.0)    # 前后转向架各取一点：车身沿轨道弯
	var b := rail_at(tram_s + 3.0)
	var c := rail_at(tram_s)
	var fwd: Vector3 = ((b["pos"] as Vector3) - (a["pos"] as Vector3)).normalized()
	var up: Vector3 = c["up"]
	fwd = (fwd - up * fwd.dot(up)).normalized()
	tram.global_transform = Transform3D(Basis(fwd.cross(up), up, -fwd).orthonormalized(), (c["pos"] as Vector3) + up * 0.15)


func _move_tram(delta: float) -> void:
	if tram == null:
		return
	if _dwell > 0.0:
		_dwell -= delta
		return
	var prev := tram_s
	tram_s = fposmod(tram_s + tram_speed * (3.0 if _called >= 0 and not riding else 1.0) * delta, rail_len)
	if not stations.is_empty():
		var st: Dictionary = stations[_next_station]
		var target := float(st["s"])
		var passed := (prev <= target and tram_s >= target) or (prev > tram_s and (target >= prev or target <= tram_s))
		if passed:
			tram_s = target
			_dwell = 5.0
			if _called == _next_station:
				_dwell = 30.0
				_called = -1
			_say("%s站" % String(st["name"]))
			_next_station = (_next_station + 1) % stations.size()
	_place_tram()


# ───────────────────────── 信件 ─────────────────────────

func _build_letters() -> void:
	for L in game.get("letters", []):
		var pos := _v(L["pos"])
		var up := (pos - _planet_c).normalized()
		var area := Area3D.new()
		area.name = "letter_" + String(L.get("scene", ""))
		var cs := CollisionShape3D.new()
		var sp := SphereShape3D.new()
		sp.radius = 1.3
		cs.shape = sp
		area.add_child(cs)
		var env := Node3D.new()   # 信封：白色薄片 + 红色火漆
		var paper := MeshInstance3D.new()
		var pm := BoxMesh.new()
		pm.size = Vector3(0.75, 0.5, 0.05)
		paper.mesh = pm
		var mw := StandardMaterial3D.new()
		mw.albedo_color = Color(0.98, 0.96, 0.9)
		mw.emission_enabled = true
		mw.emission = Color(1.0, 0.95, 0.8)
		mw.emission_energy_multiplier = 0.25
		paper.material_override = mw
		env.add_child(paper)
		var seal := MeshInstance3D.new()
		var sm := CylinderMesh.new()
		sm.top_radius = 0.09
		sm.bottom_radius = 0.09
		sm.height = 0.04
		seal.mesh = sm
		seal.rotation = Vector3(PI / 2, 0, 0)
		seal.position = Vector3(0, 0, 0.04)
		var mr := StandardMaterial3D.new()
		mr.albedo_color = Color(0.8, 0.16, 0.14)
		seal.material_override = mr
		env.add_child(seal)
		area.add_child(env)
		var beam := MeshInstance3D.new()   # 信件上方一道淡淡的光柱：远处、全局视角都能看见（拿到后随信件一起消失）
		var bm := CylinderMesh.new()
		bm.top_radius = 0.18
		bm.bottom_radius = 0.35
		bm.height = 14.0
		bm.radial_segments = 8
		bm.cap_top = false
		bm.cap_bottom = false
		beam.mesh = bm
		beam.position = Vector3(0, 7.5, 0)
		var bmat := StandardMaterial3D.new()
		bmat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		bmat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
		bmat.albedo_color = Color(1.0, 0.93, 0.7, 0.28)
		bmat.cull_mode = BaseMaterial3D.CULL_DISABLED
		beam.material_override = bmat
		beam.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		area.add_child(beam)
		game_root.add_child(area)
		var ref := Vector3.UP if absf(up.dot(Vector3.UP)) < 0.95 else Vector3.FORWARD
		var f := (ref - up * ref.dot(up)).normalized()
		area.global_transform = Transform3D(Basis(f.cross(up), up, -f).orthonormalized(), pos)
		var rec := {"node": area, "env": env, "scene": String(L.get("scene", "")), "name": String(L.get("name", "")), "t": randf() * TAU}
		letters.append(rec)
		area.body_entered.connect(func(body): _on_letter(rec, body))
		add_lantern(pos - up * 0.9, null, 1.6, 6.0)   # 夜里信件旁有一盏小灯，好找
	total = letters.size()


func _on_letter(rec: Dictionary, body: Node) -> void:
	if body != player or not is_instance_valid(rec["node"]):
		return
	(rec["node"] as Node).queue_free()
	got += 1
	_say("拿到「%s」的信（%d / %d）" % [rec["name"], got, total] if got < total else "全部 %d 封信都找到了！" % total)
	_update_hud()


# ───────────────────────── 玩家与 HUD ─────────────────────────

func _build_player() -> void:
	player = PlanetPlayer.new()
	player.name = "tiger"
	add_child(player)
	var sp: Vector3 = spawn_point
	var g: Dictionary = game
	if g.has("spawn"):
		sp = _v(g["spawn"])
	elif not stations.is_empty():
		var st := rail_at(float(stations[0]["s"]))
		sp = nearest_walk_point((st["pos"] as Vector3))
	player.setup(_planet_c, _planet_r, sp)
	player.rescue = rescue_player


func _build_hud() -> void:
	hud = Label.new()
	hud.position = Vector2(16, 12)
	hud.add_theme_font_size_override("font_size", 22)
	add_child(hud)
	tip = Label.new()
	tip.position = Vector2(16, 44)
	tip.modulate = Color(1, 1, 1, 0.85)
	add_child(tip)
	banner = Label.new()
	banner.add_theme_font_size_override("font_size", 28)
	banner.position = Vector2(16, 80)
	add_child(banner)
	_update_hud()


func _update_hud() -> void:
	hud.text = "信件 %d / %d" % [got, total] + _letter_hint()


const _DIRS := ["前方", "右前方", "右边", "右后方", "后方", "左后方", "左边", "左前方"]

## 最近一封没拿的信在哪：按相机朝向说方位，按星球表面量距离（大圆弧长）
func _letter_hint() -> String:
	if player == null or got >= total:
		return ""
	var up: Vector3 = player.up_vec()
	var best := {}
	var best_d := INF
	for L in letters:
		if not is_instance_valid(L["node"]):
			continue
		var p: Vector3 = (L["node"] as Node3D).global_position
		var d := up.angle_to((p - _planet_c).normalized()) * _planet_r
		if d < best_d:
			best_d = d
			best = L
	if best.is_empty():
		return ""
	if best_d < 6.0:
		return "　·　「%s」的信就在附近" % best["name"]
	var to := (best["node"] as Node3D).global_position - player.global_position
	to = (to - up * to.dot(up)).normalized()
	var fwd: Vector3 = player.cam_fwd
	var ang := atan2(to.dot(fwd.cross(up)), to.dot(fwd))   # 右为正
	var idx := int(round(ang / (TAU / 8.0))) % 8
	if idx < 0:
		idx += 8
	return "　·　最近的信：「%s」 %d 米 · %s" % [best["name"], int(best_d), _DIRS[idx]]


func _say(msg: String) -> void:
	banner.text = msg
	_banner_t = 3.0


func _process(delta: float) -> void:
	super(delta)
	if not _game_ready:
		return
	_move_tram(delta)
	_orient_patrols()
	for L in letters:
		if is_instance_valid(L["node"]):
			L["t"] = float(L["t"]) + delta
			(L["env"] as Node3D).rotation = Vector3(0, float(L["t"]) * 1.6, 0)
			(L["env"] as Node3D).position = Vector3(0, 0.25 * sin(float(L["t"]) * 2.0), 0)
	if _showcase:
		return
	_hint_t -= delta
	if _hint_t <= 0.0:
		_hint_t = 0.25
		_update_hud()
	if _banner_t > 0.0:
		_banner_t -= delta
		banner.modulate.a = clampf(_banner_t, 0.0, 1.0)
	if riding:
		tip.text = ("司机视角" if driver_view else "车厢视角") + " · 拖动鼠标转视角 · C 切换视角 · F 下车"
	elif _can_board():
		tip.text = "按 F 上车"
	else:
		tip.text = "WASD 走 · 空格跳（贴墙按住扒上高台）· Q/E 转视角 · 滚轮远近 · G 全局 · T 叫电车 · F 上车 · R 脱困 · L 光照 · V 云\n每一站藏着一封信"
	if _demo:
		_run_demo(delta)


func _unhandled_input(e: InputEvent) -> void:
	super(e)
	if _game_ready and riding:
		_tram_look(e)
	if not _game_ready or _showcase or not (e is InputEventKey) or not (e as InputEventKey).pressed or (e as InputEventKey).echo:
		return
	var k := (e as InputEventKey).keycode
	if k == KEY_F:
		if riding:
			get_off()
		elif _can_board():
			get_on()
		else:
			_say("离电车太远：按 T 叫车，到最近一站的栈桥上等")
	elif k == KEY_T and not riding:
		call_tram()
	elif k == KEY_R and not riding:
		rescue_player(player.cam_fwd)
	elif k == KEY_C and riding:
		set_driver_view(not driver_view)
	elif k == KEY_L:
		_apply_lighting({"day": "dusk", "dusk": "night"}.get(lighting, "day"))
	elif k == KEY_V and _cloud_root:
		_cloud_root.visible = not _cloud_root.visible


## 车上：拖动鼠标转视角——车厢视角绕着电车转、滚轮拉远拉近；司机视角左右转头、上下看
func _tram_look(e: InputEvent) -> void:
	if e is InputEventMouseButton:
		var mb := e as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT or mb.button_index == MOUSE_BUTTON_RIGHT:
			_tram_drag = mb.pressed
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_UP and not driver_view:
			_tram_dist = maxf(_tram_dist * 0.9, 5.0)
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_DOWN and not driver_view:
			_tram_dist = minf(_tram_dist * 1.1, 60.0)
		_place_tram_cam()
	elif e is InputEventMouseMotion and _tram_drag:
		var mm := e as InputEventMouseMotion
		if driver_view:
			driver_cam.rotation.y = clampf(driver_cam.rotation.y - mm.relative.x * 0.006, -2.4, 2.4)
			driver_cam.rotation.x = clampf(driver_cam.rotation.x - mm.relative.y * 0.005, deg_to_rad(-60), deg_to_rad(35))
		else:
			_tram_yaw -= mm.relative.x * 0.008
			_tram_pitch = clampf(_tram_pitch + mm.relative.y * 0.005, 0.1, 1.35)
			_place_tram_cam()


func _place_tram_cam() -> void:
	if tram_cam == null:
		return
	var o := Vector3(sin(_tram_yaw) * cos(_tram_pitch), sin(_tram_pitch), cos(_tram_yaw) * cos(_tram_pitch)) * _tram_dist
	tram_cam.position = o + Vector3(0, 1.5, 0)
	tram_cam.look_at(tram.global_transform * Vector3(0, 1.5, 0), tram.global_basis.y)


func get_on() -> void:
	riding = true
	player.frozen = true
	player.visible = false
	player.process_mode = Node.PROCESS_MODE_DISABLED
	set_driver_view(false)
	_say("上车了：电车会在每一站停靠")


func get_off() -> void:
	riding = false
	driver_view = false
	var here: Vector3 = tram.global_position
	player.process_mode = Node.PROCESS_MODE_INHERIT
	player.place(nearest_walk_point(here) if not walk_points.is_empty() else here)
	player.visible = true
	player.frozen = false
	player.camera.current = true
	_say("下车")


func set_driver_view(on: bool) -> void:
	driver_view = on
	if on:
		driver_cam.current = true
	else:
		tram_cam.current = true


## 树木 / 道具不挡小老虎（no_collision），但要挡相机：给它们加只在第 3 层的碰撞，只有相机射线检测这一层
func _camera_blockers() -> void:
	for mi in scene_root.find_children("*", "MeshInstance3D", true, false):
		if not _ignored(mi, "no_collision") or _ignored(mi, "ignore_bounds"):
			continue
		var before := (mi as Node).get_child_count()
		_add_collision(mi as MeshInstance3D)
		if (mi as Node).get_child_count() > before:
			var body := (mi as Node).get_child(before) as StaticBody3D
			if body:
				body.collision_layer = 4
				body.collision_mask = 0


## 小老虎卡住（或按 R）：挪到附近、尽量在想走方向上的可站立点
func rescue_player(want: Vector3) -> void:
	if riding or walk_points.is_empty():
		return
	var here := player.global_position
	var up := (here - _planet_c).normalized()
	var w := (want - up * want.dot(up)).normalized()
	var best := Vector3.ZERO
	var bs := INF
	for q in walk_points:
		var v: Vector3 = (q as Vector3) - here
		var d := v.length()
		if d < 3.0 or d > 18.0:
			continue
		var score := d - 6.0 * (v / d).dot(w) + 3.0 * absf(v.dot(up))
		if score < bs:
			bs = score
			best = q
	if bs == INF:
		best = nearest_walk_point(here)
	player.place(best)
	_say("小老虎跳出来了（卡住时也可以按 R）")


func _orient_patrols() -> void:
	for p in _patrols:
		var node: Node3D = p["node"]
		node.visible = lighting == "night"
		var up := (node.global_position - _planet_c).normalized()
		var ref := Vector3.UP if absf(up.dot(Vector3.UP)) < 0.95 else Vector3.FORWARD
		var f := (ref - up * ref.dot(up)).normalized()
		node.global_basis = Basis(f.cross(up), up, -f).orthonormalized()


## 演示：自动上车、切到司机视角（流水线 / 云端截图用：--planet-demo）
func _run_demo(delta: float) -> void:
	_demo_t += delta
	if _demo_t > 1.0 and not riding:
		player.global_position = tram.global_position + (tram.global_basis.x * 3.0)
		get_on()
	if _demo_t > 3.0 and riding and not driver_view:
		set_driver_view(true)
