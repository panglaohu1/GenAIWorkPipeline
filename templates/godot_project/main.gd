extends Node3D
## 主场景：用代码搭建整个世界（比手写 .tscn 更稳）。
## 资产在 res://assets/*.glb，用 load(path).instantiate() 放进场景。

func _ready() -> void:
	_build_environment()
	_build_ground()
	var cam := Camera3D.new()
	cam.position = Vector3(0, 6, 10)
	add_child(cam)
	cam.look_at(Vector3.ZERO)

func _build_environment() -> void:
	var env := WorldEnvironment.new()
	var e := Environment.new()
	e.background_mode = Environment.BG_SKY
	var sky := Sky.new()
	sky.sky_material = ProceduralSkyMaterial.new()
	e.sky = sky
	e.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	e.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	env.environment = e
	add_child(env)
	var sun := DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(-50, 30, 0)
	sun.shadow_enabled = true
	add_child(sun)

func _build_ground() -> void:
	var ground := StaticBody3D.new()
	var mesh := MeshInstance3D.new()
	var plane := PlaneMesh.new()
	plane.size = Vector2(60, 60)
	mesh.mesh = plane
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.35, 0.5, 0.3)
	mesh.material_override = mat
	ground.add_child(mesh)
	var col := CollisionShape3D.new()
	var shape := BoxShape3D.new()
	shape.size = Vector3(60, 0.2, 60)
	col.shape = shape
	col.position.y = -0.1
	ground.add_child(col)
	add_child(ground)
