extends Area3D
## 可收集物（由 Kit.make_pickup 创建）：玩家进入时发出 collected 信号并消失。

signal collected(pickup: Area3D)

@export var spin_speed := 1.5
@export var bob_height := 0.15
var _t := 0.0
var _base_y := 0.0

func _ready() -> void:
	_base_y = position.y
	body_entered.connect(_on_body_entered)

func _process(delta: float) -> void:
	_t += delta
	rotation.y += spin_speed * delta
	position.y = _base_y + sin(_t * 2.0) * bob_height

func _on_body_entered(body: Node3D) -> void:
	if body.is_in_group("player"):
		collected.emit(self)
		queue_free()
