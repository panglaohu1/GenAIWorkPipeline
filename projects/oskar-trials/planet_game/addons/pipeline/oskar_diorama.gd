extends Node3D
## Oskar 风格场景展示（流水线 oskar_godot 阶段生成配置 res://oskar_scene.json）。
## 与 Blender 渲染同机位、同光照（参数来自 genai_pipeline/blender/look.py）；地形与建筑自动生成三角网格碰撞体。
## 交互：左键拖动 / A D 旋转，滚轮 / W S 缩放，E 切换日光 / 黄昏 / 夜晚，C 开关云。
## 云是流动的：成团的低多边形云随风漂移，遇到高处地形会抬升绕过，越界后从上风处回来（展示截图时默认关闭）。
## 夜晚：冷蓝环境 + 少量“大而柔”的暖色点光（灯笼、巡逻火把）；玩法可调用 add_lantern / add_patrol_light。
## 玩法写在 main.gd：extends 本脚本，在 _ready() 里先调用 super()。
## 流水线试运行（命令行带 --pipeline-seconds）时按配置 showcase 依次切换机位与光照，供截图对照。

var cfg := {}
var scene_root: Node3D
var center := Vector3.ZERO
var radius := 1.0
var dist := 10.0
var yaw := 0.0
var pitch := 0.3
var lighting := "day"
var cam: Camera3D
var env: Environment
var hint: Label
var _suns: Array = []
var _showcase := false
var _orbit := true
## 位置数据（Godot 坐标，Y 向上，米）：可站立点、通路（每条从低到高）、出生点
var walk_points: Array = []
var path_points: Array = []
var spawn_point := Vector3.ZERO
var _shown := -1
var _t := 0.0
var _dragging := false
var bounds := AABB()
var _clouds: Array = []          # [{node, vel, base_y, phase, half}]
var _cloud_root: Node3D
var _lanterns: Array = []        # OmniLight3D，只在夜晚（黄昏减弱）点亮
var _patrols: Array = []         # [{node, path, s, speed}]
var _planet := false             # 多场景整合成小行星时：云绕球流动
var _planet_c := Vector3.ZERO
var _planet_r := 0.0


func _ready() -> void:
	get_tree().set_meta("oskar_world", true)   # 玩家控制器据此开启自动攀台阶
	cfg = _load_cfg()
	_build_environment()
	scene_root = _load_scene()
	_frame()
	_orbit = bool(cfg.get("orbit", true))
	for a in OS.get_cmdline_user_args():
		if String(a).begins_with("--pipeline-seconds=") and bool(cfg.get("showcase", true)):
			_showcase = true
	for p in cfg.get("walk", []):
		walk_points.append(Vector3(float(p[0]), float(p[1]), float(p[2])))
	for path in cfg.get("paths", []):
		var arr: Array = []
		for p in path:
			arr.append(Vector3(float(p[0]), float(p[1]), float(p[2])))
		path_points.append(arr)
	var sp: Array = cfg.get("spawn", [0, 0, 0])
	spawn_point = Vector3(float(sp[0]), float(sp[1]), float(sp[2]))
	var views: Array = cfg.get("tour", [["hero", "day"]])
	_apply_view(String(views[0][0]))
	_apply_lighting(String(views[0][1]))
	_build_clouds()
	_build_night()
	hint = Label.new()
	hint.text = "拖动 / A D 旋转 · 滚轮 / W S 缩放 · E 日光 / 黄昏 / 夜晚 · C 云"
	hint.position = Vector2(16, 12)
	hint.modulate = Color(1, 1, 1, 0.85)
	hint.visible = not _showcase and _orbit
	add_child(hint)


func _load_cfg() -> Dictionary:
	var f := FileAccess.open("res://oskar_scene.json", FileAccess.READ)
	if f == null:
		push_error("缺少 res://oskar_scene.json")
		return {}
	var d = JSON.parse_string(f.get_as_text())
	return d if d is Dictionary else {}


func _build_environment() -> void:
	var we := WorldEnvironment.new()
	env = Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.reflected_light_source = Environment.REFLECTION_SOURCE_BG
	env.tonemap_mode = Environment.TONE_MAPPER_LINEAR  # 对应 Blender 的 Standard 色彩映射
	env.ssao_enabled = bool(cfg.get("ssao", true))  # 柔和 AO：对应 EEVEE 的环境光遮蔽，台阶与缝隙压暗
	env.ssao_radius = float(cfg.get("ssao_radius", 1.2))
	env.ssao_intensity = float(cfg.get("ssao_intensity", 1.6))
	we.environment = env
	add_child(we)
	cam = Camera3D.new()
	cam.fov = float(cfg.get("fov", 39.6))
	add_child(cam)
	cam.current = true


func _load_scene() -> Node3D:
	var ps: PackedScene = load(String(cfg.get("glb", "res://assets/oskar_scene.glb")))
	var inst := ps.instantiate() as Node3D
	add_child(inst)
	if bool(cfg.get("vertex_srgb", true)):
		_fix_vertex_colors(inst)
	if bool(cfg.get("live_look", true)):
		_live_look(inst)
	if cfg.get("collision", true):
		for mi in inst.find_children("*", "MeshInstance3D", true, false):
			if not _ignored(mi) and not _ignored(mi, "no_collision"):
				_add_collision(mi as MeshInstance3D)
	return inst


## Blender 导出的顶点色（岩体 / 草地的纵向色带）在 Godot 里按 sRGB 存储；材质默认把它当线性值用，
## Forward+（Mac 上的 Metal）里整片发白发灰。导入后把用顶点色的材质标成 sRGB，颜色与 Blender 渲染一致。
## 只对地形（oskar_scene* / oskar_links*：tint.py 写的 BYTE_COLOR 顶点色）生效；树木道具的顶点色是线性的，
## 也标成 sRGB 会把树冠压得发黑（2026-10-07 实测）。
func _fix_vertex_colors(root: Node) -> void:
	for mi in root.find_children("*", "MeshInstance3D", true, false):
		var terrain := false
		var cur: Node = mi
		while cur != null and cur != root:
			var nm := String(cur.name)
			if nm.begins_with("oskar_scene") or nm.begins_with("oskar_link"):
				terrain = true
				break
			cur = cur.get_parent()
		if not terrain:
			continue
		var mesh := (mi as MeshInstance3D).mesh
		if mesh == null:
			continue
		for si in mesh.get_surface_count():
			var m := mesh.surface_get_material(si) as BaseMaterial3D
			if m and m.vertex_color_use_as_albedo:
				m.vertex_color_is_srgb = true


## 运行期画面（2026-10-07，用户：描边太重、水面波纹不动）：
##  · 描边：Blender 的反转外壳固定 7 厘米，近看像粗黑框。改为按到相机距离算的细线——外壳往回收，
##    只留约 1.6 像素宽（近处最细 1 厘米），颜色比原描边浅一些、带一点暖色。Oskar：“柔和色彩 + 清晰细线”，
##    线的粗细 / 浓淡决定吸引多少注意力（knowledge: 1482011744843223042）。
##  · 海面白色波纹线：沿岸一圈圈明暗交替地“呼吸”，相位随位置变化，像浪一层层推上岸；
##  · 岸边泡沫：缓慢明暗起伏；
##  · 外海：极轻的随时间流动的明暗带，不改变海的主色。
const OUTLINE_SHADER := """
shader_type spatial;
render_mode unshaded, cull_back, shadows_disabled;
uniform vec4 line_color : source_color = vec4(0.2, 0.18, 0.18, 1.0);
uniform float hull = 0.07;
uniform float px = 0.0016;
uniform float min_w = 0.01;
void vertex() {
	vec3 vp = (MODELVIEW_MATRIX * vec4(VERTEX, 1.0)).xyz;
	float w = clamp(length(vp) * px, min_w, hull);
	VERTEX += NORMAL * (hull - w);
}
void fragment() { ALBEDO = line_color.rgb; }
"""
const RIPPLE_SHADER := """
shader_type spatial;
render_mode unshaded, cull_disabled, depth_draw_never, shadows_disabled;
uniform vec4 tint : source_color = vec4(1.0);
uniform float speed = 0.9;
varying vec3 wpos;
void vertex() { wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz; }
void fragment() {
	float ph = dot(wpos, vec3(0.21, 0.17, 0.23)) + length(wpos) * 0.35;
	float a = 0.25 + 0.75 * smoothstep(-0.2, 0.8, sin(TIME * speed - ph));
	ALBEDO = tint.rgb;
	ALPHA = tint.a * a;
}
"""
const OCEAN_SHADER := """
shader_type spatial;
render_mode cull_back, depth_draw_always;
uniform vec4 base : source_color = vec4(0.2, 0.55, 0.65, 1.0);
uniform vec4 shallow : source_color = vec4(0.55, 0.86, 0.84, 1.0);
uniform float shallow_depth = 2.2;   // 水下多深以内算浅滩（米）
uniform float foam_depth = 0.35;     // 贴岸一圈白沫的宽度（米）
uniform float roughness_v = 0.35;
uniform sampler2D depth_tex : hint_depth_texture, filter_nearest;
varying vec3 wpos;
void vertex() { wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz; }
void fragment() {
	float b = sin(dot(wpos, vec3(0.11, 0.07, 0.09)) * 3.0 + TIME * 0.6) * sin(dot(wpos, vec3(-0.05, 0.12, 0.08)) * 4.0 - TIME * 0.45);
	// 岸边浅滩：读场景深度，水面下离岩体越近颜色越浅（Bad North 岛边那一圈亮色），贴岸再加一道会呼吸的白沫
	float d = texture(depth_tex, SCREEN_UV).r;
#if CURRENT_RENDERER == RENDERER_COMPATIBILITY
	d = d * 2.0 - 1.0;
#endif
	vec4 vp = INV_PROJECTION_MATRIX * vec4(SCREEN_UV * 2.0 - 1.0, d, 1.0);
	float under = (-vp.z / vp.w) - (-VERTEX.z);
	float sh = 1.0 - smoothstep(0.0, shallow_depth, under);
	vec3 col = mix(base.rgb, shallow.rgb, sh * 0.75);
	float fw = foam_depth * (1.0 + 0.35 * sin(TIME * 1.3 + dot(wpos, vec3(0.3, 0.2, 0.25))));
	float foam = 1.0 - smoothstep(fw * 0.6, fw, under);
	col = mix(col, vec3(0.97, 0.98, 0.96), foam * 0.85);
	ALBEDO = col * (1.0 + 0.05 * b);
	ALPHA = 1.0;   // 走透明通道才能读深度图；仍是不透明的观感
	ROUGHNESS = roughness_v;
	SPECULAR = 0.3;
}
"""


func _live_look(root: Node) -> void:
	var oc: Dictionary = cfg.get("outline_live", {})
	var outline_sh := Shader.new()
	outline_sh.code = OUTLINE_SHADER
	var ripple_sh := Shader.new()
	ripple_sh.code = RIPPLE_SHADER
	var ocean_sh := Shader.new()
	ocean_sh.code = OCEAN_SHADER
	var cache := {}
	for mi in root.find_children("*", "MeshInstance3D", true, false):
		var mesh := (mi as MeshInstance3D).mesh
		if mesh == null:
			continue
		for si in mesh.get_surface_count():
			var m := mesh.surface_get_material(si) as BaseMaterial3D
			if m == null:
				continue
			var nm := String(m.resource_name)
			var sm: ShaderMaterial = cache.get(m)
			if sm == null:
				if nm.begins_with("osk_outline"):
					sm = ShaderMaterial.new()
					sm.shader = outline_sh
					var c := m.albedo_color.lerp(Color(0.42, 0.34, 0.3), float(oc.get("soften", 0.35)))
					sm.set_shader_parameter("line_color", c)
					sm.set_shader_parameter("hull", float(oc.get("hull", 0.07)))
					sm.set_shader_parameter("px", float(oc.get("px", 0.0016)))
				elif nm.begins_with("osk_ripple") or nm.begins_with("osk_foam"):
					sm = ShaderMaterial.new()
					sm.shader = ripple_sh
					var t := m.albedo_color
					sm.set_shader_parameter("tint", Color(t.r, t.g, t.b, maxf(t.a, 0.85)))
					sm.set_shader_parameter("speed", 0.9 if nm.begins_with("osk_ripple") else 0.55)
				elif nm.begins_with("osk_ocean"):
					sm = ShaderMaterial.new()
					sm.shader = ocean_sh
					sm.set_shader_parameter("base", m.albedo_color)
					sm.set_shader_parameter("roughness_v", m.roughness)
				else:
					continue
				cache[m] = sm
			(mi as MeshInstance3D).set_surface_override_material(si, sm)


## 三角网格碰撞，跳过描边外壳（材质名含 outline）：外壳贴着所有表面外 7 厘米，算进碰撞会把玩家卡住
func _add_collision(mi: MeshInstance3D) -> void:
	var mesh := mi.mesh
	var faces := PackedVector3Array()
	for si in mesh.get_surface_count():
		var mat := mesh.surface_get_material(si)
		if mat and String(mat.resource_name).findn("outline") >= 0:
			continue
		var arr := mesh.surface_get_arrays(si)
		var verts: PackedVector3Array = arr[Mesh.ARRAY_VERTEX]
		var idx = arr[Mesh.ARRAY_INDEX]
		if idx == null or (idx as PackedInt32Array).is_empty():
			faces.append_array(verts)
		else:
			for i in (idx as PackedInt32Array):
				faces.append(verts[i])
	if faces.is_empty():
		return
	var shape := ConcavePolygonShape3D.new()
	shape.set_faces(faces)
	var body := StaticBody3D.new()
	var cs := CollisionShape3D.new()
	cs.shape = shape
	body.add_child(cs)
	mi.add_child(body)


func _ignored(n: Node, key := "ignore_bounds") -> bool:
	var names: Array = cfg.get(key, [])
	var cur: Node = n
	while cur != null and cur != self:
		for nm in names:   # 前缀匹配：多场景整合时导入的同名节点会带后缀（oskar_sea_001 等）
			if String(cur.name).begins_with(String(nm)):
				return true
		cur = cur.get_parent()
	return false


func _frame() -> void:
	var box := AABB()
	var first := true
	for mi in scene_root.find_children("*", "MeshInstance3D", true, false):
		if _ignored(mi):
			continue
		var b: AABB = (mi as MeshInstance3D).global_transform * (mi as MeshInstance3D).get_aabb()
		box = b if first else box.merge(b)
		first = false
	center = box.get_center()
	bounds = box
	radius = maxf(box.size.length() / 2.0, 0.05)
	dist = radius / tan(deg_to_rad(cam.fov) / 2.0) * float(cfg.get("frame_margin", 1.05))
	cam.far = radius * 100.0
	print("OSKAR_BOUNDS " + JSON.stringify({"center": [center.x, center.y, center.z],
		"size": [box.size.x, box.size.y, box.size.z], "dist": dist}))


func _apply_view(view_name: String) -> void:
	var d: Array = cfg.get("views", {}).get(view_name, [0.33, 0.25, 0.9])
	var dir := Vector3(float(d[0]), float(d[1]), float(d[2])).normalized()
	yaw = atan2(dir.x, dir.z)
	pitch = asin(clampf(dir.y, -1.0, 1.0))
	_update_cam()


func _update_cam() -> void:
	var dir := Vector3(sin(yaw) * cos(pitch), sin(pitch), cos(yaw) * cos(pitch))
	cam.position = center + dir * dist
	cam.look_at(center, Vector3.UP)


func _apply_lighting(preset: String) -> void:
	lighting = preset
	var L: Dictionary = cfg.get("lighting", {}).get(preset, {})
	var bg: Array = L.get("bg", [0.5, 0.5, 0.5])
	var s := float(L.get("bg_strength", 1.0))
	var c := Color(float(bg[0]) * s, float(bg[1]) * s, float(bg[2]) * s).linear_to_srgb()
	env.background_color = c
	env.ambient_light_color = c
	env.ambient_light_energy = float(cfg.get("ambient_scale", 1.0))
	_apply_fog(c, preset)
	# 灯光只建一次，切换预设时只改颜色 / 强度 / 方向：运行中增删带阴影的灯会触发渲染管线重编译，
	# 实测首次运行时 Metal 会在这一步卡死（2026-09-29，试运行在切到黄昏时超时）
	var lights: Array = L.get("lights", [])
	while _suns.size() < lights.size():
		var nl := DirectionalLight3D.new()
		nl.shadow_enabled = true
		nl.light_angular_distance = 0.5
		add_child(nl)
		_suns.append(nl)
	for i in _suns.size():
		(_suns[i] as DirectionalLight3D).visible = i < lights.size()
	for i in lights.size():
		var l: Dictionary = lights[i]
		var sun := _suns[i] as DirectionalLight3D
		var col: Array = l.get("color", [1, 1, 1])
		sun.light_color = Color(float(col[0]), float(col[1]), float(col[2])).linear_to_srgb()
		sun.light_energy = float(l.get("energy", 1.0))
		var d: Array = l["dir"]  # 光线传播方向（Godot 坐标）；灯光沿自身 -Z 照射
		var dv := Vector3(float(d[0]), float(d[1]), float(d[2])).normalized()
		var up := Vector3.UP if absf(dv.y) < 0.99 else Vector3.FORWARD
		sun.look_at_from_position(Vector3.ZERO, dv, up)
	var lamp := 1.0 if preset == "night" else (0.35 if preset == "dusk" else 0.0)
	for l in _lanterns:
		(l as OmniLight3D).visible = lamp > 0.0
		(l as OmniLight3D).light_energy = float(l.get_meta("energy", 2.0)) * lamp
	for cl in _clouds:   # 夜里云偏冷偏暗，黄昏偏暖
		var m: StandardMaterial3D = cl["mat"]
		m.albedo_color = {"night": Color(0.42, 0.48, 0.62), "dusk": Color(1.0, 0.86, 0.78)}.get(preset, Color(1, 1, 1))


func _apply_fog(sky: Color, preset: String) -> void:
	# 空气透视：远景统一向天色（背景色）淡出，让各站的地色、植被、水在远处落进同一层空气里，画面更和谐。
	# 只有显式给了 fog_density（星球游戏）才开；单场景展示 / 评测渲染不加雾，免得和 Blender 渲染对不上
	var fd := float(cfg.get("fog_density", (cfg.get("game", {}) as Dictionary).get("fog_density", 0.0)))
	var args := " ".join(OS.get_cmdline_user_args())
	if args.contains("--pipeline-seconds=") and not args.contains("--planet-demo"):
		fd = 0.0   # 流水线试运行要和 Blender 渲染逐张核对相似度，不加雾
	env.fog_enabled = fd > 0.0
	if fd <= 0.0:
		return
	env.fog_mode = Environment.FOG_MODE_EXPONENTIAL
	env.fog_density = fd * (1.35 if preset == "night" else 1.0)
	env.fog_light_color = sky
	env.fog_light_energy = 1.0
	env.fog_sun_scatter = 0.12 if preset == "dusk" else 0.04   # 黄昏迎光一侧更暖更亮
	env.fog_sky_affect = 0.0   # 背景本身已是天色，不再叠雾


func _process(delta: float) -> void:
	_move_clouds(delta)
	_move_patrols(delta)
	if _showcase:
		_t += delta
		var views: Array = cfg.get("tour", [])
		if views.is_empty():
			return
		var i := clampi(int(_t / float(cfg.get("showcase_step", 2.0))), 0, views.size() - 1)
		if i != _shown:
			_shown = i
			_apply_view(String(views[i][0]))
			if String(views[i][1]) != lighting:
				_apply_lighting(String(views[i][1]))
		return
	if not _orbit:
		return
	var turn := Input.get_axis("move_left", "move_right")
	var zoom := Input.get_axis("move_forward", "move_back")
	if turn != 0.0 or zoom != 0.0:
		yaw += turn * delta * 1.2
		dist = clampf(dist * (1.0 + zoom * delta), radius * 0.6, radius * 8.0)
		_update_cam()


func _unhandled_input(e: InputEvent) -> void:
	if _showcase or not _orbit:
		return
	if e is InputEventMouseButton:
		var mb := e as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			_dragging = mb.pressed
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_UP:
			dist = maxf(dist * 0.9, radius * 0.6)
			_update_cam()
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			dist = minf(dist * 1.1, radius * 8.0)
			_update_cam()
	elif e is InputEventMouseMotion and _dragging:
		var mm := e as InputEventMouseMotion
		yaw -= mm.relative.x * 0.008
		pitch = clampf(pitch + mm.relative.y * 0.006, 0.05, 1.45)
		_update_cam()
	elif e.is_action_pressed("action"):
		_apply_lighting({"day": "dusk", "dusk": "night"}.get(lighting, "day"))
	elif e is InputEventKey and (e as InputEventKey).pressed and (e as InputEventKey).keycode == KEY_C and _cloud_root:
		_cloud_root.visible = not _cloud_root.visible


# ───────────────────────── 流动的云 ─────────────────────────

func _build_clouds() -> void:
	var pl: Dictionary = cfg.get("planet", {})
	if not pl.is_empty():
		var pc: Array = pl.get("center", [0, 0, 0])
		_planet = true
		_planet_c = Vector3(float(pc[0]), float(pc[1]), float(pc[2]))
		_planet_r = float(pl.get("radius", radius * 0.5))
	var c: Dictionary = cfg.get("clouds", {})
	var n := int(c.get("count", 0))
	if n <= 0 or (_showcase and not bool(c.get("in_showcase", false))):
		return
	_cloud_root = Node3D.new()
	_cloud_root.name = "oskar_clouds"
	add_child(_cloud_root)
	var rng := RandomNumberGenerator.new()
	rng.seed = int(c.get("seed", 11))
	var w: Array = c.get("wind", [1.0, 0.0, 0.35])
	var wind := Vector3(float(w[0]), 0.0, float(w[2])).normalized()
	var speed := float(c.get("speed", 1.0)) * maxf(radius / 30.0, 0.4)
	var half := maxf(bounds.size.x, bounds.size.z) * 0.75
	var top := bounds.position.y + bounds.size.y
	for i in n:
		var cloud := Node3D.new()
		var mat := StandardMaterial3D.new()
		mat.albedo_color = Color(1, 1, 1)
		mat.roughness = 1.0
		mat.emission_enabled = true          # 云自身略亮，背光处不发灰
		mat.emission = Color(0.55, 0.6, 0.68)
		mat.emission_energy_multiplier = 0.35
		var size := rng.randf_range(0.06, 0.11) * radius
		if _planet:   # 星球上的云是小团块（相对星球半径），不遮住场景
			size = rng.randf_range(0.05, 0.085) * _planet_r
		for k in rng.randi_range(3, 6):      # 一团云 = 3–6 个压扁的低多边形球
			var mi := MeshInstance3D.new()
			var sm := SphereMesh.new()
			sm.radial_segments = 9
			sm.rings = 5
			sm.radius = size * rng.randf_range(0.55, 1.0)
			sm.height = sm.radius * 2.0
			mi.mesh = sm
			mi.material_override = mat
			mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			mi.scale = Vector3(1.0, rng.randf_range(0.42, 0.6), rng.randf_range(0.8, 1.1))
			mi.position = Vector3(rng.randf_range(-1.4, 1.4) * size, rng.randf_range(0.0, 0.35) * size, rng.randf_range(-0.7, 0.7) * size)
			cloud.add_child(mi)
		var base_y := top + rng.randf_range(0.05, 0.25) * radius
		cloud.position = center + Vector3(rng.randf_range(-half, half), 0.0, rng.randf_range(-half, half))
		cloud.position.y = base_y
		_cloud_root.add_child(cloud)
		var entry := {"node": cloud, "vel": wind * speed * rng.randf_range(0.7, 1.3), "base_y": base_y,
			"phase": rng.randf() * TAU, "half": half, "size": size, "mat": mat}
		if _planet:   # 星球：云在球壳上绕倾斜的轴流动，云底朝向球心
			var shell := _planet_r * rng.randf_range(1.3, 1.45)   # 高于大多数建筑，遇到高峰再由射线抬升
			var dir := Vector3(rng.randf_range(-1, 1), rng.randf_range(-1, 1), rng.randf_range(-1, 1)).normalized()
			var axis := Vector3(rng.randf_range(-0.35, 0.35), 1.0, rng.randf_range(-0.35, 0.35)).normalized()
			entry["shell"] = shell
			entry["dir"] = dir
			entry["axis"] = axis
			entry["omega"] = speed * rng.randf_range(0.7, 1.3) / shell
			cloud.position = _planet_c + dir * shell
		_clouds.append(entry)


func _move_clouds(delta: float) -> void:
	if _clouds.is_empty() or not _cloud_root.visible:
		return
	var space := get_world_3d().direct_space_state
	if _planet:
		_move_planet_clouds(delta, space)
		return
	for c in _clouds:
		var node: Node3D = c["node"]
		node.position += (c["vel"] as Vector3) * delta
		var rel := node.position - center
		var half: float = c["half"]
		if absf(rel.x) > half or absf(rel.z) > half:   # 越界：从上风处回来
			var v: Vector3 = (c["vel"] as Vector3).normalized()
			var keep_y := node.position.y
			node.position = center - v * half * 0.98 + Vector3(-v.z, 0.0, v.x) * rel.dot(Vector3(-v.z, 0.0, v.x))
			node.position.y = keep_y
		c["phase"] = float(c["phase"]) + delta * 0.6
		# 地形避让：向下探测，云底至少高出地面 0.6 个云团尺寸（翻山而过，不穿山）
		var target_y: float = float(c["base_y"]) + sin(float(c["phase"])) * float(c["size"]) * 0.15
		var hit := space.intersect_ray(PhysicsRayQueryParameters3D.create(node.position + Vector3.UP * radius, node.position + Vector3.DOWN * radius * 2.0))
		if hit:
			target_y = maxf(target_y, (hit["position"] as Vector3).y + float(c["size"]) * 0.9)
		node.position.y = lerpf(node.position.y, target_y, clampf(delta * 0.8, 0.0, 1.0))
		var breathe := 1.0 + sin(float(c["phase"]) * 0.7) * 0.05   # 缓慢起伏的形状，看起来在流动
		node.scale = Vector3(breathe, 1.0 / breathe, breathe)


func _move_planet_clouds(delta: float, space: PhysicsDirectSpaceState3D) -> void:
	for c in _clouds:
		var node: Node3D = c["node"]
		var dir: Vector3 = (c["dir"] as Vector3).rotated(c["axis"], float(c["omega"]) * delta).normalized()
		c["dir"] = dir
		c["phase"] = float(c["phase"]) + delta * 0.6
		var size: float = c["size"]
		var target: float = float(c["shell"]) + sin(float(c["phase"])) * size * 0.15
		# 地形避让：从云的外侧朝球心探测，云底至少高出地表 0.9 个云团尺寸（翻山而过）
		var outer := _planet_c + dir * (float(c["shell"]) + radius)
		var hit := space.intersect_ray(PhysicsRayQueryParameters3D.create(outer, _planet_c))
		if hit:
			target = maxf(target, ((hit["position"] as Vector3) - _planet_c).length() + size * 0.9)
		var r: float = c.get("r", float(c["shell"]))
		r = lerpf(r, target, clampf(delta * 0.8, 0.0, 1.0))
		c["r"] = r
		node.position = _planet_c + dir * r
		var fwd := (c["axis"] as Vector3).cross(dir).normalized()
		if fwd.length() > 0.01:
			node.basis = Basis(fwd.cross(dir).normalized(), dir, -fwd).orthonormalized()
		var breathe := 1.0 + sin(float(c["phase"]) * 0.7) * 0.05
		node.scale = Vector3(breathe, 1.0 / breathe, breathe)


# ───────────────────────── 夜晚：大而柔的暖光 ─────────────────────────

## 在 pos 放一盏灯笼光；follow 不为空时挂在该节点上（如士兵）。只在夜晚亮、黄昏弱亮。
func add_lantern(pos: Vector3, follow: Node3D = null, energy := 2.2, light_range := 0.0) -> OmniLight3D:
	var l := OmniLight3D.new()
	l.light_color = Color(1.0, 0.64, 0.34)
	l.omni_range = light_range if light_range > 0.0 else maxf(radius * 0.22, 6.0)   # 大
	l.omni_attenuation = 0.7                                                       # 柔
	l.shadow_enabled = false
	l.set_meta("energy", energy)
	l.visible = lighting == "night"
	l.light_energy = energy if lighting == "night" else 0.0
	if follow:
		follow.add_child(l)
		l.position = Vector3(0, 1.6, 0)
	else:
		add_child(l)
		l.position = pos + Vector3(0, 1.4, 0)
	_lanterns.append(l)
	return l


## 一名提灯巡逻的士兵（占位：小立柱 + 暖光），沿 path（Vector3 数组）来回走；玩法可替换成真正的士兵模型
func add_patrol_light(path: Array, speed := 1.4) -> Node3D:
	if path.size() < 2:
		return null
	var guard := Node3D.new()
	guard.name = "night_patrol"
	var body := MeshInstance3D.new()
	var cap := CapsuleMesh.new()
	cap.radius = 0.25
	cap.height = 1.5
	body.mesh = cap
	body.position = Vector3(0, 0.75, 0)
	guard.add_child(body)
	add_child(guard)
	guard.position = path[0]
	add_lantern(Vector3.ZERO, guard, 2.6)
	_patrols.append({"node": guard, "path": path, "s": 0.0, "speed": speed, "dir": 1, "idx": 0})
	return guard


func _build_night() -> void:
	var nc: Dictionary = cfg.get("night", {})
	var n := int(nc.get("lanterns", 0))
	var spots: Array = []
	for path in path_points:   # 灯笼放在通路两端（门口、桥头、码头），其次是离出生点较远的可站立点
		if (path as Array).size() > 0:
			spots.append(path[0])
			spots.append(path[path.size() - 1])
	for p in pick_walk_points(n, maxf(radius * 0.12, 4.0), 3):
		spots.append(p)
	var placed: Array = []
	for p in spots:
		if placed.size() >= n:
			break
		var ok := true
		for q in placed:
			if (q as Vector3).distance_to(p) < radius * 0.12:
				ok = false
		if ok:
			placed.append(p)
			add_lantern(p)
	if bool(nc.get("patrol", false)) and not path_points.is_empty():
		var longest: Array = path_points[0]
		for path in path_points:
			if (path as Array).size() > longest.size():
				longest = path
		add_patrol_light(longest)


func _move_patrols(delta: float) -> void:
	for p in _patrols:   # 沿通路走到头就折返；idx 为当前段起点，dir 为方向
		var path: Array = p["path"]
		var node: Node3D = p["node"]
		var idx: int = p.get("idx", 0)
		var dir: int = p["dir"]
		if idx + dir < 0 or idx + dir >= path.size():
			dir = -dir
			p["dir"] = dir
		var a: Vector3 = path[idx]
		var b: Vector3 = path[idx + dir]
		var t := float(p["s"]) + float(p["speed"]) * delta / maxf(a.distance_to(b), 0.01)
		if t >= 1.0:
			p["idx"] = idx + dir
			t = 0.0
			node.position = b
		else:
			node.position = a.lerp(b, t)
		p["s"] = t


## 挑 n 个彼此相距 ≥ min_dist 的可站立点（离出生点远的优先），适合放收集物；点在地面上，放物件时自己加高度
func pick_walk_points(n: int, min_dist := 4.0, seed := 7) -> Array:
	var rng := RandomNumberGenerator.new()
	rng.seed = seed
	var pool := walk_points.duplicate()
	pool.sort_custom(func(a, b): return a.distance_to(spawn_point) > b.distance_to(spawn_point))
	var out: Array = []
	for p in pool:
		if out.size() >= n:
			break
		if p.distance_to(spawn_point) < min_dist or rng.randf() < 0.35:
			continue
		var ok := true
		for q in out:
			if p.distance_to(q) < min_dist:
				ok = false
				break
		if ok:
			out.append(p)
	return out


## 离 p 最近的可站立点
func nearest_walk_point(p: Vector3) -> Vector3:
	var best := spawn_point
	var bd := INF
	for q in walk_points:
		var d: float = q.distance_to(p)
		if d < bd:
			bd = d
			best = q
	return best
