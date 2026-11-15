extends CharacterBody3D
## 第三人称玩家控制器（由 Kit.make_player 创建）。WASD/方向键移动，空格跳跃，相机在身后跟随。

@export var speed := 5.0
@export var jump_velocity := 5.5
@export var turn_speed := 10.0
@export var camera_distance := 5.0
@export var camera_height := 2.5
## Oskar 世界（1 层 = 2 米）：被不高于 climb_height 的台阶挡住时自动攀上（Bad North 式），世界处处可达
@export var auto_climb := false
@export var climb_height := 2.3

var model: Node3D
var camera: Camera3D

func _ready() -> void:
	add_to_group("player")
	floor_max_angle = deg_to_rad(50.0)   # Oskar 世界的台阶是约 45° 的坡道
	floor_snap_length = 0.4
	if get_tree().has_meta("oskar_world"):
		auto_climb = true
	camera = Camera3D.new()
	camera.name = "FollowCamera"
	camera.top_level = true
	add_child(camera)
	camera.current = true
	_update_camera(1.0)

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity += get_gravity() * delta
	if Input.is_action_just_pressed("jump") and is_on_floor():
		velocity.y = jump_velocity
	var input := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	# 相对相机水平朝向移动
	var basis_y := Basis(Vector3.UP, camera.global_rotation.y) if camera else Basis.IDENTITY
	var dir := (basis_y * Vector3(input.x, 0, input.y)).normalized()
	if dir.length() > 0.01:
		velocity.x = dir.x * speed
		velocity.z = dir.z * speed
		if model:
			var target_yaw := atan2(dir.x, dir.z)
			model.rotation.y = lerp_angle(model.rotation.y, target_yaw, clamp(turn_speed * delta, 0.0, 1.0))
	else:
		velocity.x = move_toward(velocity.x, 0, speed)
		velocity.z = move_toward(velocity.z, 0, speed)
	move_and_slide()
	if auto_climb and dir.length() > 0.01 and is_on_floor() and is_on_wall():
		_try_climb(dir)
	_update_camera(delta)

func _try_climb(dir: Vector3) -> void:
	# 前方 0.6 米处从上往下探：落点比脚下高 0.3–climb_height 米就起跳攀上
	var ahead := global_position + dir * 0.6
	var q := PhysicsRayQueryParameters3D.create(ahead + Vector3(0, climb_height + 0.3, 0), ahead + Vector3(0, 0.05, 0))
	q.exclude = [get_rid()]
	var hit := get_world_3d().direct_space_state.intersect_ray(q)
	if hit.is_empty():
		return
	var rise: float = hit.position.y - global_position.y
	if rise > 0.3 and rise <= climb_height:
		velocity.y = sqrt(2.0 * get_gravity().length() * (rise + 0.35))

func _update_camera(delta: float) -> void:
	if camera == null:
		return
	var target := global_position + Vector3(0, camera_height, camera_distance)
	camera.global_position = camera.global_position.lerp(target, clamp(6.0 * delta, 0.0, 1.0))
	camera.look_at(global_position + Vector3(0, 0.8, 0), Vector3.UP)
