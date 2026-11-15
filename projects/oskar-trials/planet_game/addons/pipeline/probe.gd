extends Node
## 流水线探针（autoload）。仅在命令行带 --pipeline-* 用户参数时生效：
##   -- --pipeline-seconds=6 --pipeline-shots=res_dir --pipeline-inputs=<json>
## 功能：注册默认输入动作；按时间模拟输入；定时截图；记录 "player" 组节点轨迹；结束时打印 PIPELINE_PROBE JSON 并退出。

var _active := false
var _seconds := 6.0
var _shot_dir := ""
var _shot_times: Array = []
var _inputs: Array = []
var _t := 0.0
var _shots_taken: Array = []
var _track: Array = []
var _pressed := {}
var _frames := 0

const DEFAULT_ACTIONS := {
	"move_left": [KEY_A, KEY_LEFT], "move_right": [KEY_D, KEY_RIGHT],
	"move_forward": [KEY_W, KEY_UP], "move_back": [KEY_S, KEY_DOWN],
	"jump": [KEY_SPACE], "action": [KEY_E, KEY_ENTER], "sprint": [KEY_SHIFT],
}

func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	for action in DEFAULT_ACTIONS:
		if not InputMap.has_action(action):
			InputMap.add_action(action)
			for k in DEFAULT_ACTIONS[action]:
				var ev := InputEventKey.new()
				ev.physical_keycode = k
				InputMap.action_add_event(action, ev)
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--pipeline-seconds="):
			_seconds = float(arg.get_slice("=", 1)); _active = true
		elif arg.begins_with("--pipeline-shots="):
			_shot_dir = arg.get_slice("=", 1); _active = true
		elif arg.begins_with("--pipeline-shot-times="):
			for s in arg.get_slice("=", 1).split(","):
				_shot_times.append(float(s))
		elif arg.begins_with("--pipeline-inputs="):
			var parsed = JSON.parse_string(arg.substr(len("--pipeline-inputs=")))
			if parsed is Array:
				_inputs = parsed
	if _active and _shot_times.is_empty():
		_shot_times = [max(_seconds - 0.5, 0.5)]

func _process(delta: float) -> void:
	if not _active:
		return
	_t += delta
	_frames += 1
	for inp in _inputs:
		var a: String = inp.get("action", "")
		var st: float = float(inp.get("start", 0.0))
		var du: float = float(inp.get("duration", 0.5))
		var on := _t >= st and _t < st + du
		if on and not _pressed.get(a, false):
			Input.action_press(a); _pressed[a] = true
		elif not on and _pressed.get(a, false):
			Input.action_release(a); _pressed[a] = false
	if _frames % 10 == 0:
		for p in get_tree().get_nodes_in_group("player"):
			if p is Node3D:
				_track.append({"t": snappedf(_t, 0.01), "name": p.name, "pos": [snappedf(p.global_position.x, 0.01), snappedf(p.global_position.y, 0.01), snappedf(p.global_position.z, 0.01)]})
	while not _shot_times.is_empty() and _t >= float(_shot_times[0]):
		var ts: float = float(_shot_times.pop_front())
		_take_shot(ts)
	if _t >= _seconds:
		_finish()

func _take_shot(ts: float) -> void:
	if _shot_dir == "":
		return
	await RenderingServer.frame_post_draw
	var img := get_viewport().get_texture().get_image()
	var path := "%s/shot_%05.2f.png" % [_shot_dir, ts]
	img.save_png(path)
	_shots_taken.append(path)

func _finish() -> void:
	_active = false
	await RenderingServer.frame_post_draw
	var report := {
		"seconds": snappedf(_t, 0.01), "frames": _frames, "fps": Engine.get_frames_per_second(),
		"node_count": get_tree().get_node_count(), "shots": _shots_taken, "player_track": _track,
		"players": get_tree().get_nodes_in_group("player").size(),
	}
	print("PIPELINE_PROBE " + JSON.stringify(report))
	get_tree().quit()
