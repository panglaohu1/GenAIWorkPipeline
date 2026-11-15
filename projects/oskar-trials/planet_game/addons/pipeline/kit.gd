extends RefCounted
## 游戏搭建工具箱。用法：const Kit = preload("res://addons/pipeline/kit.gd")
## 所有尺寸单位米；资产 GLB 原点在底部中心、Y 轴向上。

const PlayerController = preload("res://addons/pipeline/player_controller.gd")
const Pickup = preload("res://addons/pipeline/pickup.gd")

## 加载 GLB 并实例化
static func load_asset(path: String) -> Node3D:
	var scene: PackedScene = load(path)
	return scene.instantiate() as Node3D

## 计算节点（含子节点网格）的包围盒，坐标相对 node 自身
static func aabb_of(node: Node3D) -> AABB:
	var result := AABB()
	var first := true
	for mi in node.find_children("*", "MeshInstance3D", true, false):
		var t := Transform3D.IDENTITY
		var cur: Node = mi
		while cur != null and cur != node:
			if cur is Node3D:
				t = (cur as Node3D).transform * t
			cur = cur.get_parent()
		var box: AABB = t * (mi as MeshInstance3D).get_aabb()
		result = box if first else result.merge(box)
		first = false
	return result

## 等比缩放到指定高度（米）
static func fit_height(node: Node3D, height: float) -> void:
	var h := aabb_of(node).size.y
	if h > 0.0001 and height > 0:
		node.scale = Vector3.ONE * (height / h)

## 放置一个静态装饰/建筑资产。height<=0 表示保持原尺寸；collide=true 生成盒形碰撞
static func spawn(parent: Node, path: String, pos: Vector3, height := -1.0, rot_y_deg := 0.0, collide := true) -> Node3D:
	var model := load_asset(path)
	if height > 0:
		fit_height(model, height)
	if not collide:
		parent.add_child(model)
		model.position = pos
		model.rotation_degrees.y = rot_y_deg
		return model
	var body := StaticBody3D.new()
	body.name = path.get_file().get_basename()
	parent.add_child(body)
	body.position = pos
	body.rotation_degrees.y = rot_y_deg
	body.add_child(model)
	var box := aabb_of(model)
	var col := CollisionShape3D.new()
	var shape := BoxShape3D.new()
	shape.size = box.size * model.scale
	col.shape = shape
	col.position = box.get_center() * model.scale
	body.add_child(col)
	return body

## 创建玩家：CharacterBody3D + 胶囊碰撞 + 第三人称跟随相机，已加入 "player" 组
static func make_player(parent: Node, model_path: String, pos: Vector3, height := 1.0) -> CharacterBody3D:
	var p := CharacterBody3D.new()
	p.name = "Player"
	p.set_script(PlayerController)
	var model := load_asset(model_path)
	fit_height(model, height)
	p.add_child(model)
	p.model = model
	var box := aabb_of(model)
	var radius: float = max(0.15, min(box.size.x, box.size.z) * model.scale.x * 0.45)
	var col := CollisionShape3D.new()
	var cap := CapsuleShape3D.new()
	cap.radius = radius
	cap.height = max(height, radius * 2.0 + 0.01)
	col.shape = cap
	col.position.y = cap.height / 2.0
	p.add_child(col)
	parent.add_child(p)
	p.position = pos
	return p

## 创建可收集物：玩家触碰时发出 collected 信号并消失。用 pickup.collected.connect(func(x): ...)
static func make_pickup(parent: Node, model_path: String, pos: Vector3, height := 0.4, group := "pickup") -> Area3D:
	var a := Area3D.new()
	a.set_script(Pickup)
	a.add_to_group(group)
	var model := load_asset(model_path)
	fit_height(model, height)
	a.add_child(model)
	var col := CollisionShape3D.new()
	var sphere := SphereShape3D.new()
	sphere.radius = max(0.5, height)
	col.shape = sphere
	col.position.y = height / 2.0
	a.add_child(col)
	parent.add_child(a)
	a.position = pos
	return a

## 创建触发区（如终点），玩家进入时调用 on_enter.call(body)
static func make_trigger(parent: Node, pos: Vector3, size: Vector3, on_enter: Callable) -> Area3D:
	var a := Area3D.new()
	var col := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = size
	col.shape = box
	col.position.y = size.y / 2.0
	a.add_child(col)
	parent.add_child(a)
	a.position = pos
	a.body_entered.connect(func(body: Node3D) -> void:
		if body.is_in_group("player"):
			on_enter.call(body))
	return a

## 天空 + 太阳 + 环境光
static func add_environment(parent: Node, sky_top := Color(0.45, 0.65, 0.95), ground_color := Color(0.4, 0.5, 0.35)) -> void:
	var env := WorldEnvironment.new()
	var e := Environment.new()
	e.background_mode = Environment.BG_SKY
	var sky := Sky.new()
	var mat := ProceduralSkyMaterial.new()
	mat.sky_top_color = sky_top
	mat.ground_bottom_color = ground_color
	sky.sky_material = mat
	e.sky = sky
	e.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	e.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	env.environment = e
	parent.add_child(env)
	var sun := DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(-50, 30, 0)
	sun.shadow_enabled = true
	parent.add_child(sun)

## 带碰撞的平地
static func add_ground(parent: Node, size := 40.0, color := Color(0.35, 0.55, 0.3)) -> StaticBody3D:
	var ground := StaticBody3D.new()
	ground.name = "Ground"
	var mesh := MeshInstance3D.new()
	var plane := PlaneMesh.new()
	plane.size = Vector2(size, size)
	mesh.mesh = plane
	var mat := StandardMaterial3D.new()
	mat.albedo_color = color
	mesh.material_override = mat
	ground.add_child(mesh)
	var col := CollisionShape3D.new()
	var shape := BoxShape3D.new()
	shape.size = Vector3(size, 0.2, size)
	col.shape = shape
	col.position.y = -0.1
	ground.add_child(col)
	parent.add_child(ground)
	return ground

## 屏幕文字（HUD）。返回 Label，之后改 label.text 即可
static func make_label(parent: Node, text := "", pos := Vector2(20, 20), font_size := 28) -> Label:
	var layer := CanvasLayer.new()
	parent.add_child(layer)
	var label := Label.new()
	label.text = text
	# 默认字体不含中文：使用工程自带的 Noto Sans CJK SC（SIL OFL）
	label.add_theme_font_override("font", load("res://addons/pipeline/fonts/NotoSansCJKsc-Regular.otf"))
	label.position = pos
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_outline_color", Color.BLACK)
	label.add_theme_constant_override("outline_size", 6)
	layer.add_child(label)
	return label

## ── 程序化装饰（竹子、石头、灌木、柱子等，不需要 GLB）──
## 均返回 MeshInstance3D（collide=true 时返回带碰撞的 StaticBody3D）；pos 为底部中心
static func _prim(parent: Node, mesh: Mesh, pos: Vector3, color: Color, y_offset: float, collide: bool, shape: Shape3D) -> Node3D:
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	var mat := StandardMaterial3D.new()
	mat.albedo_color = color
	mi.material_override = mat
	mi.position.y = y_offset
	if not collide:
		parent.add_child(mi)
		mi.position += pos
		return mi
	var body := StaticBody3D.new()
	parent.add_child(body)
	body.position = pos
	body.add_child(mi)
	var col := CollisionShape3D.new()
	col.shape = shape
	col.position.y = y_offset
	body.add_child(col)
	return body

static func cylinder(parent: Node, pos: Vector3, radius := 0.1, height := 1.0, color := Color(0.4, 0.7, 0.3), collide := false, top_radius := -1.0) -> Node3D:
	var m := CylinderMesh.new()
	m.bottom_radius = radius
	m.top_radius = radius if top_radius < 0 else top_radius
	m.height = height
	var s := CylinderShape3D.new()
	s.radius = radius
	s.height = height
	return _prim(parent, m, pos, color, height / 2.0, collide, s)

static func cone(parent: Node, pos: Vector3, radius := 0.3, height := 0.6, color := Color(0.3, 0.6, 0.3), collide := false) -> Node3D:
	return cylinder(parent, pos, radius, height, color, collide, 0.0)

static func box(parent: Node, pos: Vector3, size := Vector3.ONE, color := Color(0.6, 0.6, 0.6), collide := false) -> Node3D:
	var m := BoxMesh.new()
	m.size = size
	var s := BoxShape3D.new()
	s.size = size
	return _prim(parent, m, pos, color, size.y / 2.0, collide, s)

static func sphere(parent: Node, pos: Vector3, radius := 0.3, color := Color(0.3, 0.6, 0.3), collide := false) -> Node3D:
	var m := SphereMesh.new()
	m.radius = radius
	m.height = radius * 2.0
	var s := SphereShape3D.new()
	s.radius = radius
	return _prim(parent, m, pos, color, radius, collide, s)

## 竹子：一根竹竿（分节）+ 顶部叶团
static func bamboo(parent: Node, pos: Vector3, height := 4.0, color := Color(0.45, 0.72, 0.3)) -> Node3D:
	var root := Node3D.new()
	parent.add_child(root)
	root.position = pos
	var seg := 4
	for i in seg:
		var c := cylinder(root, Vector3(0, height * i / seg, 0), 0.07, height / seg - 0.03, color.darkened(0.05 * (i % 2)))
		c.name = "seg%d" % i
	sphere(root, Vector3(0, height - 0.3, 0), 0.6, Color(0.35, 0.65, 0.25))
	return root

## 在矩形区域内随机散布：make.call(parent, pos) 返回节点；避开 avoid 列表中各点 radius 范围
static func scatter(parent: Node, make: Callable, count: int, area_min: Vector2, area_max: Vector2, avoid: Array = [], radius := 2.0, seed := 7) -> Array:
	var rng := RandomNumberGenerator.new()
	rng.seed = seed
	var out := []
	var tries := 0
	while out.size() < count and tries < count * 30:
		tries += 1
		var p := Vector3(rng.randf_range(area_min.x, area_max.x), 0, rng.randf_range(area_min.y, area_max.y))
		var ok := true
		for a in avoid:
			if Vector2(p.x, p.z).distance_to(Vector2(a.x, a.z)) < radius:
				ok = false
				break
		if ok:
			out.append(make.call(parent, p))
	return out

## 地图导航预览（参考 AllCityKitty 六边形高尔夫：虚线抛物轨迹 + 目标格高亮 + 剩余步数）：
## var nav = Kit.make_nav_preview(self)；每帧或选中目标时调用 Kit.update_nav_preview(nav, 起点, 终点, 剩余步数)；
## 不需要时 nav.visible = false。只是视觉提示，不负责寻路。
static func make_nav_preview(parent: Node, color := Color(1, 1, 1, 0.9), dots := 14) -> Node3D:
	var root := Node3D.new()
	root.name = "nav_preview"
	var mat := StandardMaterial3D.new()
	mat.albedo_color = color
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.no_depth_test = true            # 被地形挡住时也看得见轨迹
	mat.render_priority = 2
	for i in dots:
		var d := MeshInstance3D.new()
		var sm := SphereMesh.new()
		sm.radius = 0.09
		sm.height = 0.18
		sm.radial_segments = 6
		sm.rings = 3
		d.mesh = sm
		d.material_override = mat
		d.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		d.name = "dot_%d" % i
		root.add_child(d)
	var ring := MeshInstance3D.new()
	var tm := TorusMesh.new()
	tm.inner_radius = 0.55
	tm.outer_radius = 0.68
	tm.rings = 6
	tm.ring_segments = 6                 # 六边形的目标圈
	ring.mesh = tm
	ring.material_override = mat
	ring.name = "target_ring"
	root.add_child(ring)
	var label := Label3D.new()
	label.name = "steps"
	label.billboard = BaseMaterial3D.BILLBOARD_ENABLED
	label.no_depth_test = true
	label.font_size = 48
	label.modulate = color
	root.add_child(label)
	parent.add_child(root)
	return root


static func update_nav_preview(nav: Node3D, from: Vector3, to: Vector3, steps_left := -1, arc_height := -1.0) -> void:
	var dots: Array = nav.get_children().filter(func(c): return String(c.name).begins_with("dot_"))
	var h := arc_height if arc_height > 0.0 else clampf(from.distance_to(to) * 0.35, 0.6, 6.0)
	for i in dots.size():
		var t := float(i + 1) / float(dots.size() + 1)
		var p := from.lerp(to, t)
		p.y += 4.0 * h * t * (1.0 - t)      # 抛物线
		(dots[i] as Node3D).position = p
	var ring := nav.get_node("target_ring") as Node3D
	ring.position = to + Vector3(0, 0.08, 0)
	var label := nav.get_node("steps") as Label3D
	label.visible = steps_left >= 0
	label.text = str(steps_left)
	label.position = to + Vector3(0, 1.4, 0)
	nav.visible = true
