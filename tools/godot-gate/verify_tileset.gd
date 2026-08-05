extends SceneTree

# Sprint 8 的 TileSet 真机门槛。
#
# 结构断言（tests/integration/test_tileset.py）证明不了「Godot 能加载」，
# 而「能加载」也证明不了「每一格指向它该指向的 tile」—— 图集坐标写反、
# 少写 `列:行/0 = 0` 那一行，.tres 照样能加载，只是编辑器里选不中或选错格。
#
# 所以这里验五层：
#   A. 资源本身 —— 加载成 TileSet、tile_size、每个格坐标真的存在
#   B. 纹理衔接 —— ext_resource 指向的 png 真的在（整目录复制的前提）
#   C. 图集像素 —— 每格区域与原 tile png 逐字节一致
#   D. 地图往回读 —— set_cell 后再 get_cell_atlas_coords 逐格比对
#   E. terrain —— set 数量/mode/名称，以及每格 terrain 与四个 peering bit
#
# expected.json 由 tools/godot-gate/make_expected.py 从 Manifest + 导出物生成。

func _initialize() -> void:
	var failures: Array = []
	var expected: Dictionary = JSON.parse_string(
		FileAccess.get_file_as_string("res://expected.json")
	)
	var asset_id: String = expected["asset_id"]

	# -- A. 资源本身 --------------------------------------------------------
	var loaded: Resource = ResourceLoader.load("res://%s_tileset.tres" % asset_id)
	if loaded == null:
		push_error("GATE-FAIL TileSet 加载返回 null")
		quit(1)
		return
	if not (loaded is TileSet):
		push_error("GATE-FAIL 加载出来的不是 TileSet，而是 %s" % loaded.get_class())
		quit(1)
		return

	var tileset: TileSet = loaded
	var want_size: Vector2i = Vector2i(int(expected["tile_w"]), int(expected["tile_h"]))
	if tileset.tile_size != want_size:
		failures.append("tile_size %s ≠ %s" % [tileset.tile_size, want_size])

	if tileset.get_source_count() != 1:
		failures.append("图集来源 %d 个，期望 1 个" % tileset.get_source_count())
		_report(failures)
		return

	var source_id: int = tileset.get_source_id(0)
	var source: TileSetAtlasSource = tileset.get_source(source_id) as TileSetAtlasSource
	if source == null:
		failures.append("来源不是 TileSetAtlasSource")
		_report(failures)
		return
	if source.texture_region_size != want_size:
		failures.append("texture_region_size %s ≠ %s" % [source.texture_region_size, want_size])
	if source.texture == null:
		failures.append("图集纹理为 null —— .tres 与 png 必须整目录复制")

	# 每个 tile 的格坐标都必须真的存在。少写 `列:行/0 = 0` 那一行时，
	# 图集里明明有图，Godot 却认为那一格是空的 —— 编辑器里选不中。
	var coords: Dictionary = expected["coords"]
	for tile_id in coords.keys():
		var xy: Array = coords[tile_id]
		var cell: Vector2i = Vector2i(int(xy[0]), int(xy[1]))
		if not source.has_tile(cell):
			failures.append("%s 的格 %s 在 TileSet 里不存在" % [tile_id, cell])
	print("GATE TileSet 载入成功：%d 块 tile，格 %s" % [coords.size(), want_size])

	# -- B. 纹理衔接 --------------------------------------------------------
	if not ResourceLoader.exists("res://%s.png" % asset_id):
		failures.append("图集 png 不在 res:// 下")
	var filter_mode: int = ProjectSettings.get_setting(
		"rendering/textures/canvas_textures/default_texture_filter", -1)
	if filter_mode != 0:
		failures.append("项目默认纹理过滤是 %d，不是 Nearest(0) —— 像素会被糊掉" % filter_mode)

	# -- C. 图集那一格里装的确实是那块 tile ---------------------------------
	#
	# 前面只验了"这些格存在"，而那对**格坐标整体转置**恒判通过 —— 实测 3 块 tile
	# 摆成 2×2 时坐标集 {(0,0),(1,0),(0,1)} 转置后等于自己，"哪些格存在"根本
	# 区分不了。要抓住它只能比像素：把图集在该格的区域切出来，与这块 tile 自己的
	# png 逐字节比。
	#
	# 这一层需要 tiles/<tile_id>.png（从资产的 frames/tiles/ 复制进来）。
	var atlas_image: Image = source.texture.get_image()
	atlas_image.convert(Image.FORMAT_RGBA8)
	for tile_id in coords.keys():
		var reference_path: String = "res://tiles/%s.png" % tile_id
		if not ResourceLoader.exists(reference_path):
			failures.append("缺少参照图 %s —— 这一层验不了" % reference_path)
			continue
		var reference: Image = (ResourceLoader.load(reference_path) as Texture2D).get_image()
		reference.convert(Image.FORMAT_RGBA8)

		var xy: Array = coords[tile_id]
		var cell: Vector2i = Vector2i(int(xy[0]), int(xy[1]))
		var region: Image = atlas_image.get_region(
			Rect2i(cell * want_size, want_size)
		)
		region.convert(Image.FORMAT_RGBA8)
		if region.get_data() != reference.get_data():
			failures.append(
				"图集格 %s 里装的不是 %s —— 格坐标算错了（转置？打包顺序？）" % [cell, tile_id]
			)

	# -- D. 地图往回读 ------------------------------------------------------
	var rows: Array = expected["rows"]
	if rows.is_empty():
		failures.append("expected.json 里没有地图，第四层验不了")
	else:
		var layer: TileMapLayer = TileMapLayer.new()
		layer.tile_set = tileset
		for y in rows.size():
			var row: Array = rows[y]
			for x in row.size():
				var xy: Array = coords[row[x]]
				layer.set_cell(Vector2i(x, y), source_id, Vector2i(int(xy[0]), int(xy[1])))

		var mismatches: int = 0
		for y in rows.size():
			var row: Array = rows[y]
			for x in row.size():
				var got: Vector2i = layer.get_cell_atlas_coords(Vector2i(x, y))
				var xy: Array = coords[row[x]]
				var want: Vector2i = Vector2i(int(xy[0]), int(xy[1]))
				if got != want:
					mismatches += 1
					if mismatches <= 3:
						failures.append("地图 (%d,%d) 读回 %s，期望 %s（%s）" % [
							x, y, got, want, row[x]])
				if layer.get_cell_source_id(Vector2i(x, y)) != source_id:
					mismatches += 1
		if mismatches == 0:
			print("GATE 地图 %d×%d 逐格读回一致" % [rows[0].size(), rows.size()])
		layer.free()

	# -- E. terrain set 与逐格 peering bits ----------------------------------
	var expected_terrain: Variant = expected.get("terrain")
	if expected_terrain == null:
		if tileset.get_terrain_sets_count() != 0:
			failures.append("Manifest 没有 terrain，TileSet 却有 %d 个 terrain set" % tileset.get_terrain_sets_count())
		print("GATE terrain：Manifest 未启用，本层确认 TileSet 没有擅自添加")
	else:
		var terrain_expected: Dictionary = expected_terrain
		var want_sets: int = int(terrain_expected["sets_count"])
		var terrain_names: Array = terrain_expected["names"]
		if tileset.get_terrain_sets_count() != want_sets:
			failures.append("terrain set 数量 %d ≠ %d" % [tileset.get_terrain_sets_count(), want_sets])
		if want_sets > 0 and tileset.get_terrain_sets_count() > 0:
			var want_mode: int = int(terrain_expected["mode"])
			var got_mode: int = tileset.get_terrain_set_mode(0)
			if got_mode != want_mode:
				failures.append("terrain set 0 mode %d ≠ %d（Corners）" % [got_mode, want_mode])

			if tileset.get_terrains_count(0) != terrain_names.size():
				failures.append("terrain 数量 %d ≠ %d" % [tileset.get_terrains_count(0), terrain_names.size()])
			for terrain_index in min(tileset.get_terrains_count(0), terrain_names.size()):
				var got_name: String = tileset.get_terrain_name(0, terrain_index)
				if got_name != terrain_names[terrain_index]:
					failures.append("terrain %d 名称 %s ≠ %s" % [terrain_index, got_name, terrain_names[terrain_index]])

		var terrain_tiles: Dictionary = terrain_expected["tiles"]
		var corner_bits := [
			["top_left", TileSet.CELL_NEIGHBOR_TOP_LEFT_CORNER],
			["top_right", TileSet.CELL_NEIGHBOR_TOP_RIGHT_CORNER],
			["bottom_left", TileSet.CELL_NEIGHBOR_BOTTOM_LEFT_CORNER],
			["bottom_right", TileSet.CELL_NEIGHBOR_BOTTOM_RIGHT_CORNER],
		]
		for tile_id in terrain_tiles.keys():
			var xy: Array = coords[tile_id]
			var cell := Vector2i(int(xy[0]), int(xy[1]))
			var tile_data: TileData = source.get_tile_data(cell, 0)
			var tile_expected: Dictionary = terrain_tiles[tile_id]
			if bool(tile_expected["skipped"]):
				if tile_data.get_terrain_set() != -1 or tile_data.get_terrain() != -1:
					failures.append("%s 含 unknown，却读到 terrain_set=%d terrain=%d" % [
						tile_id, tile_data.get_terrain_set(), tile_data.get_terrain()])
				for corner in corner_bits:
					# terrain_set=-1 时直接 get 会由 Godot 打一条“invalid peering bit”错误；
					# is_valid 才是确认整格没标注、且不制造假错误日志的公开 API。
					if tile_data.is_valid_terrain_peering_bit(corner[1]):
						failures.append("%s 含 unknown，%s peering bit 却仍有效" % [tile_id, corner[0]])
				continue

			if tile_data.get_terrain_set() != 0:
				failures.append("%s terrain_set %d ≠ 0" % [tile_id, tile_data.get_terrain_set()])
			var want_terrain: int = terrain_names.find(tile_expected["terrain"])
			if tile_data.get_terrain() != want_terrain:
				failures.append("%s terrain %d ≠ %d（%s）" % [
					tile_id, tile_data.get_terrain(), want_terrain, tile_expected["terrain"]])

			var measured: Array = tile_expected["measured_corners"]
			for corner_index in corner_bits.size():
				var corner: Array = corner_bits[corner_index]
				var want_bit: int = terrain_names.find(measured[corner_index])
				var got_bit: int = tile_data.get_terrain_peering_bit(corner[1])
				if got_bit != want_bit:
					failures.append("%s/%s peering bit %d ≠ %d（%s）" % [
						tile_id, corner[0], got_bit, want_bit, measured[corner_index]])
		print("GATE terrain 读回：%d 个 set，逐格 terrain/四角 peering bits 已核对" % want_sets)

	_report(failures)


func _report(failures: Array) -> void:
	if failures.is_empty():
		print("GATE-OK TileSet 五层全部通过")
		quit(0)
	else:
		for f in failures:
			push_error("GATE-FAIL %s" % f)
		quit(1)
