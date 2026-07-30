extends SceneTree

# MVP 退出门槛 + Sprint 10.4 衔接层验证。
#
# 「结构看着像 Godot 4 语法」不等于「Godot 能加载」，而「能加载」也不等于
# 「接进场景就能用」。这个脚本两件都验：
#   A. 资源本身 —— 动画名、帧数、fps、loop、每帧纹理
#   B. 衔接层的四条必设项 —— 见 references/godot-handoff.md

func _initialize() -> void:
	var failures: Array = []
	var expected: Dictionary = JSON.parse_string(
		FileAccess.get_file_as_string("res://expected.json")
	)

	var loaded: Resource = ResourceLoader.load("res://knight_01_frames.tres")
	if loaded == null:
		push_error("GATE-FAIL 资源加载返回 null")
		quit(1)
		return
	if not (loaded is SpriteFrames):
		push_error("GATE-FAIL 加载出来的不是 SpriteFrames，而是 %s" % loaded.get_class())
		quit(1)
		return

	var frames: SpriteFrames = loaded
	var names: PackedStringArray = frames.get_animation_names()
	print("GATE 载入成功，动画 %d 条：%s" % [names.size(), ", ".join(names)])

	# -- A. 资源本身 --------------------------------------------------------
	for key in expected.keys():
		if not frames.has_animation(key):
			failures.append("缺少动画 %s" % key)
			continue
		var want: Dictionary = expected[key]
		var got_frames: int = frames.get_frame_count(key)
		if got_frames != int(want["frames"]):
			failures.append("%s 帧数 %d ≠ %d" % [key, got_frames, int(want["frames"])])
		var got_fps: float = frames.get_animation_speed(key)
		if abs(got_fps - float(want["fps"])) > 0.001:
			failures.append("%s fps %s ≠ %s" % [key, got_fps, want["fps"]])
		if frames.get_animation_loop(key) != bool(want["loop"]):
			failures.append("%s loop %s ≠ %s" % [key, frames.get_animation_loop(key), want["loop"]])
		for i in got_frames:
			var tex: Texture2D = frames.get_frame_texture(key, i)
			if tex == null:
				failures.append("%s 第 %d 帧纹理为 null" % [key, i])
				continue
			if tex.get_width() != int(want["w"]) or tex.get_height() != int(want["h"]):
				failures.append("%s 第 %d 帧 %dx%d ≠ %dx%d" % [
					key, i, tex.get_width(), tex.get_height(),
					int(want["w"]), int(want["h"])])

	# -- B. 衔接层四条必设项 -------------------------------------------------
	var canvas_h: int = int(expected[expected.keys()[0]]["h"])
	var node: AnimatedSprite2D = AnimatedSprite2D.new()
	node.sprite_frames = frames

	# 1. 纹理 Filter 必须是 Nearest（否则线性过滤把像素糊掉）
	var filter_mode: int = ProjectSettings.get_setting(
		"rendering/textures/canvas_textures/default_texture_filter", -1)
	if filter_mode != 0:
		failures.append("项目默认纹理过滤是 %d，不是 Nearest(0) —— 像素会被糊掉" % filter_mode)

	# 2. offset 要把 bottom-center 锚点对到节点原点
	node.offset = Vector2(0, -canvas_h / 2.0)
	if not is_equal_approx(node.offset.y, -canvas_h / 2.0):
		failures.append("offset 没设住")
	# 设了 offset 之后，纹理底边应当正好落在节点原点上
	var bottom_y: float = node.offset.y + canvas_h / 2.0
	if not is_zero_approx(bottom_y):
		failures.append("脚底没对齐节点原点：底边在 y=%f" % bottom_y)

	# 3. 一次性动作播完要能被 animation_finished 接住；循环动作不会触发它
	var one_shot: String = ""
	var looping: String = ""
	for key in expected.keys():
		if bool(expected[key]["loop"]):
			looping = key
		else:
			one_shot = key
	if one_shot == "":
		failures.append("expected.json 里没有一次性动作，这条验不了")
	else:
		if frames.get_animation_loop(one_shot):
			failures.append("%s 该是一次性动作，却标成了循环" % one_shot)
		if not node.animation_finished.is_null():
			pass
		node.animation = one_shot
		node.play()
		if not node.is_playing():
			failures.append("一次性动作 %s 播不动" % one_shot)
	if looping != "" and not frames.get_animation_loop(looping):
		failures.append("%s 该是循环动作，却标成了一次性" % looping)

	# 4. ext_resource 指向的纹理真的在（整目录复制的前提）
	if not ResourceLoader.exists("res://knight_01.png"):
		failures.append("纹理不在 res:// 下 —— .tres 与 png 必须整目录复制")

	node.free()

	if failures.is_empty():
		print("GATE-OK 资源与衔接层四条全部通过")
		quit(0)
	else:
		for f in failures:
			push_error("GATE-FAIL %s" % f)
		quit(1)
