extends SceneTree

# 让当前 Godot 二进制自己给出 terrain 相关枚举；后续还会用公开 API 生成、保存并
# 读回一份 TileSet。这个探针刻意独立于 Python 导出器，避免用同一份假设验证自己。

func _initialize() -> void:
	print("PROBE Godot %s" % Engine.get_version_info()["string"])
	for constant_name in ClassDB.class_get_integer_constant_list("TileSet"):
		var name: String = constant_name
		if "TERRAIN_MODE" in name or "CORNER" in name:
			print("PROBE %s=%d" % [
				name,
				ClassDB.class_get_integer_constant("TileSet", name),
			])

	var tileset := TileSet.new()
	tileset.tile_size = Vector2i(32, 32)
	tileset.add_terrain_set(0)
	tileset.set_terrain_set_mode(0, TileSet.TERRAIN_MODE_MATCH_CORNERS)
	for terrain_name in ["grass", "dirt"]:
		var terrain_index := tileset.get_terrains_count(0)
		tileset.add_terrain(0, terrain_index)
		tileset.set_terrain_name(0, terrain_index, terrain_name)

	var image := Image.create_empty(96, 32, false, Image.FORMAT_RGBA8)
	image.fill(Color(0.25, 0.5, 0.25, 1.0))
	var source := TileSetAtlasSource.new()
	source.texture = ImageTexture.create_from_image(image)
	source.texture_region_size = Vector2i(32, 32)
	for x in 3:
		source.create_tile(Vector2i(x, 0))
	tileset.add_source(source, 0)

	# mixed 不设置 terrain，只设置 terrain_set 与四角。保存后的文本会显示 Godot 认为
	# 这组公开 API 对应哪些键；读回值则证明这些键是否真的被引擎接受。
	var mixed := source.get_tile_data(Vector2i(0, 0), 0)
	mixed.terrain_set = 0
	_set_corners(mixed, [0, 0, 1, 1])
	var grass := source.get_tile_data(Vector2i(1, 0), 0)
	grass.terrain_set = 0
	_set_corners(grass, [0, 0, 0, 0])
	var dirt := source.get_tile_data(Vector2i(2, 0), 0)
	dirt.terrain_set = 0
	dirt.terrain = 1
	_set_corners(dirt, [1, 1, 1, 1])

	var output := "user://terrain_format_probe.tres"
	var save_error := ResourceSaver.save(tileset, output)
	if save_error != OK:
		push_error("PROBE save failed: %s" % error_string(save_error))
		quit(1)
		return
	print("PROBE saved=%s" % ProjectSettings.globalize_path(output))

	var loaded := ResourceLoader.load(output, "", ResourceLoader.CACHE_MODE_IGNORE) as TileSet
	if loaded == null:
		push_error("PROBE reload returned null")
		quit(1)
		return
	var loaded_source := loaded.get_source(0) as TileSetAtlasSource
	for x in 3:
		var tile_data := loaded_source.get_tile_data(Vector2i(x, 0), 0)
		print("PROBE tile=%d terrain_set=%d terrain=%d corners=%s" % [
			x,
			tile_data.terrain_set,
			tile_data.terrain,
			_get_corners(tile_data),
		])

	# 用只有同质草 tile、且未设置 terrain 的对照资源试一次 terrain-connect。若它仍能
	# 被选中，terrain 不是 Corners 模式匹配所需的输入；反之则是必要字段。
	loaded_source.remove_tile(Vector2i(0, 0))
	loaded_source.remove_tile(Vector2i(2, 0))
	var layer := TileMapLayer.new()
	layer.tile_set = loaded
	layer.set_cells_terrain_connect([Vector2i.ZERO], 0, 0, false)
	print("PROBE connect_without_terrain=%s" % layer.get_cell_atlas_coords(Vector2i.ZERO))
	layer.free()

	var loaded_grass := loaded_source.get_tile_data(Vector2i(1, 0), 0)
	loaded_grass.terrain = 0
	var connected_layer := TileMapLayer.new()
	connected_layer.tile_set = loaded
	connected_layer.set_cells_terrain_connect([Vector2i.ZERO], 0, 0, false)
	print("PROBE connect_with_terrain=%s" % connected_layer.get_cell_atlas_coords(Vector2i.ZERO))
	connected_layer.free()
	quit(0)


func _set_corners(tile_data: TileData, values: Array) -> void:
	var neighbors := [
		TileSet.CELL_NEIGHBOR_TOP_LEFT_CORNER,
		TileSet.CELL_NEIGHBOR_TOP_RIGHT_CORNER,
		TileSet.CELL_NEIGHBOR_BOTTOM_LEFT_CORNER,
		TileSet.CELL_NEIGHBOR_BOTTOM_RIGHT_CORNER,
	]
	for index in neighbors.size():
		tile_data.set_terrain_peering_bit(neighbors[index], int(values[index]))


func _get_corners(tile_data: TileData) -> Array:
	return [
		tile_data.get_terrain_peering_bit(TileSet.CELL_NEIGHBOR_TOP_LEFT_CORNER),
		tile_data.get_terrain_peering_bit(TileSet.CELL_NEIGHBOR_TOP_RIGHT_CORNER),
		tile_data.get_terrain_peering_bit(TileSet.CELL_NEIGHBOR_BOTTOM_LEFT_CORNER),
		tile_data.get_terrain_peering_bit(TileSet.CELL_NEIGHBOR_BOTTOM_RIGHT_CORNER),
	]
