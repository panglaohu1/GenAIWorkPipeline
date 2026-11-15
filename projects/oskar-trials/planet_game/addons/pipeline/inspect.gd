extends SceneTree
## 用法: godot --headless --path P --script res://addons/pipeline/inspect.gd -- res://assets/x.glb ...
## 打印每个资产的节点树与合并包围盒，供模型确定缩放与摆放。

func _init() -> void:
	var out := {}
	for path in OS.get_cmdline_user_args():
		var res = load(path)
		if res == null:
			out[path] = {"error": "load failed"}
			continue
		var inst: Node = res.instantiate()
		root.add_child(inst)
		var aabb := AABB()
		var first := true
		var meshes := 0
		var names := []
		for n in inst.find_children("*", "MeshInstance3D", true, false):
			var mi := n as MeshInstance3D
			meshes += 1
			if names.size() < 30:
				names.append(String(mi.name))
			var box: AABB = _rel_xform(inst, mi) * mi.get_aabb()
			if first:
				aabb = box; first = false
			else:
				aabb = aabb.merge(box)
		out[path] = {"meshes": meshes, "mesh_names": names,
			"aabb_position": [aabb.position.x, aabb.position.y, aabb.position.z],
			"aabb_size": [aabb.size.x, aabb.size.y, aabb.size.z]}
		inst.queue_free()
	print("PIPELINE_INSPECT " + JSON.stringify(out))
	quit()

# SceneTree 脚本里节点尚未进入处理循环，global_transform 不可靠：手动累乘局部变换
func _rel_xform(top: Node, n: Node) -> Transform3D:
	var t := Transform3D.IDENTITY
	var cur: Node = n
	while cur != null and cur != top.get_parent():
		if cur is Node3D:
			t = (cur as Node3D).transform * t
		cur = cur.get_parent()
	return t
