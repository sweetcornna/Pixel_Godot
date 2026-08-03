extends Node2D

@onready var knight: AnimatedSprite2D = $Knight


func _ready() -> void:
	knight.animation_finished.connect(_on_animation_finished)
	knight.play(&"attack_down")


func _on_animation_finished() -> void:
	if knight.animation == &"attack_down":
		knight.play(&"walk_down")
