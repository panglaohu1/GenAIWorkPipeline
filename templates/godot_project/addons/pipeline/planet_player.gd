extends CharacterBody3D
## 星球上的第三人称玩家（小老虎）：重力指向球心，WASD 相对相机移动，空格跳，自动攀上 ≤ 2.3 米的台阶。
## 鼠标左键拖动转相机，滚轮拉远拉近。掉进海里就游泳（空格往上蹿、顶着岸边自动扒上去），R 脱困。

var planet_center := Vector3.ZERO
var planet_radius := 40.0
var speed := 6.5
var jump_velocity := 7.5
var gravity := 22.0
var climb_height := 2.3
var cam_dist := 24.0          # Bad North / Townscaper 式：远一点、长焦，看整片台地像微缩模型
var cam_pitch := 0.85
var _yaw_pending := 0.0         # Q / E：相机绕小老虎转 45°（平滑转过去，同 Bad North）
var swim_speed := 3.6
var swimming := false
var frozen := false          # 坐上电车时由玩法脚本冻结
var model: Node3D
var camera: Camera3D
var cam_fwd := Vector3.FORWARD   # 相机在切平面里的朝向（随位置平行移动）
var _last_safe := Vector3.ZERO
var _dragging := false
var _cam_pitch_eff := 0.85
var overview := false          # G：全局视角（从高空看整颗星球，小老虎在画面中央）
var _face := Vector3.FORWARD
var rescue: Callable             # 卡住时由玩法脚本把小老虎挪到附近能走的地方
var _stuck_t := 0.0
var _probe_pos := Vector3.ZERO
var _probe_t := 0.0
var _wakes: Array = []            # 游水时身边一圈圈扩散的白色水纹
var _marker: MeshInstance3D       # 全局视角下小老虎头顶的橙色倒锥：俯瞰整颗星球时一眼找到自己
const CAMERA_MASK := 1           # 只有地形挡相机（树冠挡一下就把相机拉到老虎脸上，反而看不见）


func setup(center: Vector3, r: float, pos: Vector3) -> void:
	planet_center = center
	planet_radius = r
	add_to_group("player")
	floor_max_angle = deg_to_rad(50.0)
	floor_snap_length = 0.5
	var shape := CapsuleShape3D.new()
	shape.radius = 0.32
	shape.height = 1.0
	var cs := CollisionShape3D.new()
	cs.shape = shape
	cs.position = Vector3(0, 0.55, 0)
	add_child(cs)
	model = make_tiger()
	add_child(model)
	camera = Camera3D.new()
	camera.name = "TigerCamera"
	camera.top_level = true
	camera.fov = 38.0   # 长焦：透视变形小，台地层次清楚（Bad North 的等距感）
	camera.near = 0.05
	add_child(camera)
	_marker = MeshInstance3D.new()
	var cone := CylinderMesh.new()
	cone.top_radius = 1.0
	cone.bottom_radius = 0.0   # 尖朝下，指着小老虎
	cone.height = 2.0
	cone.radial_segments = 6
	_marker.mesh = cone
	var mm := StandardMaterial3D.new()
	mm.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mm.albedo_color = Color(1.0, 0.55, 0.12)
	mm.no_depth_test = true   # 被岛挡住也看得见
	mm.render_priority = 2
	_marker.material_override = mm
	_marker.top_level = true
	_marker.visible = false
	_marker.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(_marker)
	for k in 2:
		var w := MeshInstance3D.new()
		var tm := TorusMesh.new()
		tm.inner_radius = 0.62
		tm.outer_radius = 0.7
		tm.rings = 20
		tm.ring_segments = 4
		w.mesh = tm
		var wm := StandardMaterial3D.new()
		wm.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		wm.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
		wm.albedo_color = Color(0.97, 0.99, 1.0, 0.8)
		w.material_override = wm
		w.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		w.visible = false
		w.position = Vector3(0, 0.47, 0)   # 水面：游水时身体中心在水面下 0.45 米
		add_child(w)
		_wakes.append(w)
	place(pos)


func place(pos: Vector3) -> void:
	var up := (pos - planet_center).normalized()
	global_position = pos + up * 0.4
	_last_safe = global_position
	velocity = Vector3.ZERO
	var ref := Vector3.UP if absf(up.dot(Vector3.UP)) < 0.95 else Vector3.FORWARD
	cam_fwd = (ref - up * ref.dot(up)).normalized()
	_face = cam_fwd
	_orient(up)
	if camera:   # 相机直接就位，不从原点飞过来
		camera.global_position = global_position + up * (cam_dist * sin(cam_pitch) + 1.0) - cam_fwd * cam_dist * cos(cam_pitch)
		camera.look_at(global_position + up * 0.8, up)


func up_vec() -> Vector3:
	return (global_position - planet_center).normalized()


func _orient(up: Vector3) -> void:
	var f := (_face - up * _face.dot(up)).normalized()
	if f.length() < 0.5:
		f = cam_fwd
	global_basis = Basis(f.cross(up).normalized(), up, -f).orthonormalized()


func _physics_process(delta: float) -> void:
	if frozen:
		return
	var up := up_vec()
	up_direction = up
	cam_fwd = (cam_fwd - up * cam_fwd.dot(up)).normalized()
	var right := cam_fwd.cross(up).normalized()
	var inp := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var dir := (right * inp.x - cam_fwd * inp.y)
	if dir.length() > 1.0:
		dir = dir.normalized()
	var v_up := up * velocity.dot(up)
	var v_t := velocity - v_up
	if dir.length() > 0.05:
		v_t = dir * speed
		_face = _face.slerp(dir.normalized(), clampf(10.0 * delta, 0.0, 1.0))
	else:
		v_t = v_t.move_toward(Vector3.ZERO, speed * 8.0 * delta)
	var h0 := (global_position - planet_center).length()
	swimming = h0 < planet_radius - 0.3 and not is_on_floor()   # 海面就是星球半径处：身子没进水就游泳
	if swimming:
		if dir.length() > 0.05:
			v_t = dir * swim_speed
		# 浮力：把身体托在水面下 0.45 米（头露在外面），轻微起伏
		var target := planet_radius - 0.45 + 0.06 * sin(Time.get_ticks_msec() * 0.004)
		var vy := velocity.dot(up)
		vy = lerpf(vy, (target - h0) * 4.0, clampf(6.0 * delta, 0.0, 1.0))
		if Input.is_action_just_pressed("jump"):
			vy = jump_velocity * 0.8   # 水里也能往上一蹿，扒上岸边
		v_up = up * vy
	elif is_on_floor():
		if Input.is_action_just_pressed("jump"):
			v_up = up * jump_velocity
	else:
		v_up -= up * gravity * delta
	velocity = v_t + v_up
	move_and_slide()
	if dir.length() > 0.05 and is_on_wall():
		_try_climb(dir.normalized(), up, Input.is_action_pressed("jump") or swimming)
	_check_stuck(delta, dir)
	_orient(up)
	var h := (global_position - planet_center).length()
	if is_on_floor() and h > planet_radius + 0.25:
		_last_safe = global_position
	if h < planet_radius - 6.0:   # 卡到岛底下去了：回到最后站稳的地方
		global_position = _last_safe
		velocity = Vector3.ZERO
	_animate(delta)
	_update_camera(delta)


## 走路：对角两条腿一组前后摆（小跑），身体随步子上下轻颠，尾巴左右摇；游泳时身子压低、四条腿快速划水；
## 跳起来时腿收拢。步频跟着实际速度走，停下来时腿慢慢归位。
var _phase := 0.0
var _gait := 0.0
func _animate(delta: float) -> void:
	if model == null:
		return
	var up := up_vec()
	var spd := (velocity - up * velocity.dot(up)).length()
	var target_gait := clampf(spd / speed, 0.0, 1.0)
	_gait = lerpf(_gait, target_gait, clampf(10.0 * delta, 0.0, 1.0))
	_phase += delta * (4.0 + 9.0 * _gait) * (1.6 if swimming else 1.0)
	var legs: Array = model.get_meta("legs", [])
	var airborne := not is_on_floor() and not swimming and absf(velocity.dot(up)) > 2.5   # 小颠簸不算腾空
	for k in legs.size():
		var hip := legs[k] as Node3D
		var off := PI if k == 1 or k == 2 else 0.0   # 左前 + 右后一组，右前 + 左后一组
		var swing := sin(_phase + off) * (0.75 if swimming else 0.6) * maxf(_gait, 0.15 if swimming else 0.0)
		if airborne:
			swing = (-0.5 if k < 2 else 0.5)
		hip.rotation.x = lerpf(hip.rotation.x, swing, clampf(14.0 * delta, 0.0, 1.0))
	var bob := absf(sin(_phase)) * 0.05 * _gait
	model.position = Vector3(0, (-0.25 if swimming else 0.0) + bob, 0)
	model.rotation.x = deg_to_rad(-8.0) if swimming else lerpf(model.rotation.x, 0.0, 0.2)
	var tail := model.get_meta("tail", null) as Node3D
	if tail:
		tail.rotation.y = sin(_phase * 0.5) * (0.25 + 0.3 * _gait)
	for k in _wakes.size():
		var w := _wakes[k] as MeshInstance3D
		w.visible = swimming
		if swimming:
			var t := fposmod(Time.get_ticks_msec() / 1000.0 / 1.4 + 0.5 * k, 1.0)
			var sc := 0.7 + 1.8 * t
			w.scale = Vector3(sc, 1.0, sc)
			(w.material_override as StandardMaterial3D).albedo_color.a = 0.8 * (1.0 - t)


## 顶着墙走：前方台阶 ≤ climb_height 自动攀上；按住空格可以扒上 ≤ 4.4 米的台缘（两级台地）
func _try_climb(dir: Vector3, up: Vector3, mantle: bool) -> void:
	if velocity.dot(up) > 1.0:
		return
	var limit := 4.4 if mantle else climb_height
	var ahead := global_position + dir * 0.75
	var q := PhysicsRayQueryParameters3D.create(ahead + up * (limit + 0.4), ahead + up * 0.05)
	q.exclude = [get_rid()]
	q.collision_mask = 1
	var hit := get_world_3d().direct_space_state.intersect_ray(q)
	if hit.is_empty():
		return
	var rise: float = ((hit["position"] as Vector3) - global_position).dot(up)
	if rise > 0.3 and rise <= limit and (is_on_floor() or mantle or swimming):
		velocity = velocity - up * velocity.dot(up) + up * sqrt(2.0 * gravity * (rise + 0.45))


## 一直在推方向键却走不动（坑里、夹缝里）超过 2 秒：自动挪到附近能走的地方
func _check_stuck(delta: float, dir: Vector3) -> void:
	_probe_t += delta
	if _probe_t >= 0.5:
		var moved := global_position.distance_to(_probe_pos)
		_probe_pos = global_position
		_probe_t = 0.0
		if dir.length() > 0.05 and moved < 0.35:
			_stuck_t += 0.5
		else:
			_stuck_t = 0.0
	if _stuck_t >= 2.5 and rescue.is_valid():
		_stuck_t = 0.0
		rescue.call(dir if dir.length() > 0.05 else cam_fwd)


func _update_camera(delta: float) -> void:
	if camera == null or not camera.current:
		return
	var up := up_vec()
	if absf(_yaw_pending) > 0.0005:
		var step := _yaw_pending * clampf(6.0 * delta, 0.0, 1.0)
		cam_fwd = cam_fwd.rotated(up, step).normalized()
		_yaw_pending -= step
	if _marker:
		_marker.visible = overview
	if overview:
		if _marker:
			var s := planet_radius * 0.035
			var ref := cam_fwd
			_marker.global_transform = Transform3D(Basis(ref.cross(up).normalized() * s, up * s, -ref * s),
				global_position + up * (s * 1.4 + 1.0))
		# 整个半球装进画面：距离按视场角算（长焦 FOV 38° 时约 3.4 倍半径），小老虎在画面中央，略斜一点看出球的体积
		var d := planet_radius / sin(deg_to_rad(camera.fov) * 0.5 * 0.86)
		var far := planet_center + (up * d - cam_fwd * planet_radius * 0.35)
		camera.global_position = camera.global_position.lerp(far, clampf(3.0 * delta, 0.0, 1.0))
		camera.look_at(planet_center + up * planet_radius * 0.15, cam_fwd)
		return
	# 相机被崖壁 / 岩体挡住：先抬高俯角（从更高处往下看），抬到接近正上方还挡才拉近
	var head := global_position + up * 1.0
	var space := get_world_3d().direct_space_state
	var target := Vector3.ZERO
	var blocked := true
	var pitch := cam_pitch
	while pitch <= 1.45:
		target = global_position + up * (cam_dist * sin(pitch) + 1.0) - cam_fwd * cam_dist * cos(pitch)
		var q := PhysicsRayQueryParameters3D.create(head, target)
		q.exclude = [get_rid()]
		q.collision_mask = CAMERA_MASK
		var hit := space.intersect_ray(q)
		if hit.is_empty():
			blocked = false
			break
		if pitch >= 1.4:
			target = head + (((hit["position"] as Vector3) - head) * 0.85)
			break
		pitch = minf(pitch + 0.2, 1.45)
	_cam_pitch_eff = lerpf(_cam_pitch_eff, pitch, clampf(5.0 * delta, 0.0, 1.0)) if not blocked else pitch
	if blocked:
		camera.global_position = target
	else:
		target = global_position + up * (cam_dist * sin(_cam_pitch_eff) + 1.0) - cam_fwd * cam_dist * cos(_cam_pitch_eff)
		camera.global_position = camera.global_position.lerp(target, clampf(8.0 * delta, 0.0, 1.0))
	camera.look_at(global_position + up * 0.8, up)


func _unhandled_input(e: InputEvent) -> void:
	if frozen:
		return
	if e is InputEventKey and (e as InputEventKey).pressed and not (e as InputEventKey).echo:
		match (e as InputEventKey).keycode:
			KEY_G:
				overview = not overview
				return
			KEY_Q:
				_yaw_pending += PI / 4.0
				return
			KEY_E:
				_yaw_pending -= PI / 4.0
				return
	if e is InputEventMouseButton:
		var mb := e as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT or mb.button_index == MOUSE_BUTTON_RIGHT:
			_dragging = mb.pressed
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_UP:
			cam_dist = maxf(cam_dist * 0.9, 8.0)
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			cam_dist = minf(cam_dist * 1.1, 70.0)
	elif e is InputEventMouseMotion and _dragging:
		var mm := e as InputEventMouseMotion
		cam_fwd = cam_fwd.rotated(up_vec(), -mm.relative.x * 0.008).normalized()
		cam_pitch = clampf(cam_pitch + mm.relative.y * 0.005, 0.05, 1.35)


## 低多边形小老虎（橙身黑纹、白肚、圆耳、长尾），约 0.6 × 0.55 × 1.0 米，脚在原点
static func make_tiger() -> Node3D:
	var root := Node3D.new()
	root.name = "Tiger"
	var orange := _mat(Color(0.93, 0.52, 0.16))
	var black := _mat(Color(0.12, 0.1, 0.09))
	var white := _mat(Color(0.97, 0.94, 0.88))
	var pink := _mat(Color(0.93, 0.55, 0.55))
	_box(root, orange, Vector3(0, 0.5, 0), Vector3(0.5, 0.42, 0.8))            # 身体
	_box(root, white, Vector3(0, 0.33, 0.02), Vector3(0.4, 0.1, 0.62))         # 白肚
	for z in [-0.24, -0.04, 0.16]:                                              # 背上的黑纹
		_box(root, black, Vector3(0, 0.715, z), Vector3(0.52, 0.03, 0.07))
		_box(root, black, Vector3(0.255, 0.56, z), Vector3(0.02, 0.22, 0.07))
		_box(root, black, Vector3(-0.255, 0.56, z), Vector3(0.02, 0.22, 0.07))
	_box(root, orange, Vector3(0, 0.78, -0.48), Vector3(0.46, 0.4, 0.38))      # 头（-Z 朝前）
	_box(root, white, Vector3(0, 0.69, -0.68), Vector3(0.26, 0.16, 0.06))      # 口鼻
	_box(root, pink, Vector3(0, 0.76, -0.715), Vector3(0.07, 0.05, 0.02))      # 鼻头
	_box(root, black, Vector3(0.11, 0.86, -0.675), Vector3(0.06, 0.07, 0.02))  # 眼
	_box(root, black, Vector3(-0.11, 0.86, -0.675), Vector3(0.06, 0.07, 0.02))
	_box(root, black, Vector3(0, 0.97, -0.5), Vector3(0.3, 0.03, 0.06))        # 额头“王”字纹
	for x in [0.17, -0.17]:
		_box(root, orange, Vector3(x, 1.02, -0.44), Vector3(0.12, 0.12, 0.06))  # 耳
		_box(root, black, Vector3(x, 1.04, -0.405), Vector3(0.07, 0.07, 0.01))
	var legs: Array = []
	for p in [Vector3(0.16, 0.3, -0.28), Vector3(-0.16, 0.3, -0.28), Vector3(0.16, 0.3, 0.28), Vector3(-0.16, 0.3, 0.28)]:
		var hip := Node3D.new()   # 腿绕髋关节摆动
		hip.position = p
		root.add_child(hip)
		_box(hip, orange, Vector3(0, -0.15, 0), Vector3(0.14, 0.3, 0.14))       # 腿
		_box(hip, white, Vector3(0, -0.28, -0.01), Vector3(0.15, 0.05, 0.16))   # 白爪
		legs.append(hip)
	root.set_meta("legs", legs)
	var tail := Node3D.new()
	tail.position = Vector3(0, 0.62, 0.4)
	tail.rotation = Vector3(deg_to_rad(-35), 0, 0)
	root.add_child(tail)
	_box(tail, orange, Vector3(0, 0, 0.22), Vector3(0.08, 0.08, 0.44))
	_box(tail, black, Vector3(0, 0, 0.4), Vector3(0.09, 0.09, 0.08))
	root.set_meta("tail", tail)
	return root


static func _mat(c: Color) -> StandardMaterial3D:
	var m := StandardMaterial3D.new()
	m.albedo_color = c
	m.roughness = 0.9
	return m


static func _box(parent: Node3D, mat: Material, pos: Vector3, size: Vector3) -> MeshInstance3D:
	var mi := MeshInstance3D.new()
	var bm := BoxMesh.new()
	bm.size = size
	mi.mesh = bm
	mi.material_override = mat
	mi.position = pos
	parent.add_child(mi)
	return mi
