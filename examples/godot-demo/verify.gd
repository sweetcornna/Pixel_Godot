extends SceneTree

const MAIN_SCENE_PATH := "res://main.tscn"
const ATTACK_ANIMATION: StringName = &"attack_down"
const WALK_ANIMATION: StringName = &"walk_down"

var failures: Array[String] = []


func _initialize() -> void:
	var packed_scene := load(MAIN_SCENE_PATH) as PackedScene
	_check(packed_scene != null, "main.tscn 可加载", "main.tscn 加载失败")
	if packed_scene == null:
		_finish()
		return

	var scene := packed_scene.instantiate()
	root.add_child(scene)
	await process_frame

	var sprite := scene.get_node_or_null("Knight") as AnimatedSprite2D
	_check(sprite != null, "场景中存在 AnimatedSprite2D 节点 Knight", "场景中缺少 AnimatedSprite2D 节点 Knight")
	if sprite == null:
		scene.queue_free()
		_finish()
		return

	_check(sprite.is_playing(), "AnimatedSprite2D 正在播放", "AnimatedSprite2D 未播放")
	var texture_filter := int(ProjectSettings.get_setting(
		"rendering/textures/canvas_textures/default_texture_filter", -1
	))
	_check(texture_filter == 0, "项目纹理过滤为 Nearest(0)", "项目纹理过滤不是 Nearest(0)")
	_check(
		ResourceLoader.exists("res://knight_01_frames.tres")
		and ResourceLoader.exists("res://knight_01.png"),
		"knight_01_frames.tres 与 knight_01.png 均在项目中",
		"knight_01_frames.tres 或 knight_01.png 缺失"
	)

	var first_texture := sprite.sprite_frames.get_frame_texture(WALK_ANIMATION, 0)
	_check(first_texture != null, "walk_down 首帧纹理可加载", "walk_down 首帧纹理为空")
	if first_texture != null:
		var canvas_height := first_texture.get_height()
		var expected_offset := Vector2(0.0, -canvas_height / 2.0)
		_check(
			sprite.offset.is_equal_approx(expected_offset),
			"offset 为 %s（由真实帧高 %d 计算）" % [sprite.offset, canvas_height],
			"offset 为 %s，预期 %s（真实帧高 %d）" % [sprite.offset, expected_offset, canvas_height]
		)

	var viewport_width := int(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var viewport_height := int(ProjectSettings.get_setting("display/window/size/viewport_height"))
	var expected_center := Vector2(viewport_width / 2.0, viewport_height / 2.0)
	_check(
		sprite.position.is_equal_approx(expected_center),
		"节点位于视口中心 %s（%dx%d）" % [sprite.position, viewport_width, viewport_height],
		"节点位置为 %s，预期视口中心 %s" % [sprite.position, expected_center]
	)

	var callback := Callable(scene, "_on_animation_finished")
	_check(
		sprite.animation_finished.is_connected(callback),
		"animation_finished 已连接到 main.gd 回调",
		"animation_finished 未连接到 main.gd 回调"
	)
	_check(
		not sprite.sprite_frames.get_animation_loop(ATTACK_ANIMATION),
		"attack_down 是一次性动画",
		"attack_down 被错误标记为循环动画"
	)
	_check(
		sprite.sprite_frames.get_animation_loop(WALK_ANIMATION),
		"walk_down 是循环动画",
		"walk_down 被错误标记为一次性动画"
	)

	if sprite.animation == ATTACK_ANIMATION and sprite.is_playing():
		await sprite.animation_finished
		_check(
			sprite.animation == WALK_ANIMATION and sprite.is_playing(),
			"attack_down 结束后已回退并循环播放 walk_down",
			"attack_down 结束后未正确回退到 walk_down"
		)
	else:
		failures.append("启动后没有播放 attack_down，无法验证一次性动画回退")

	scene.queue_free()
	_finish()


func _check(condition: bool, success: String, failure: String) -> void:
	if condition:
		print("VERIFY-OK %s" % success)
	else:
		failures.append(failure)


func _finish() -> void:
	if failures.is_empty():
		print("VERIFY-OK 所有场景运行断言通过")
		quit(0)
	else:
		for failure in failures:
			push_error("VERIFY-FAIL %s" % failure)
		quit(1)
