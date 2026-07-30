"""Artifact Store、哈希与缓存。

最重要的一条不变量：**原始生成图永不覆盖**（PLAN §11）。
它一旦被破坏，``process`` 的离线重跑能力就没了 —— 每次调参都要重新花钱。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models.job import Job, JobKind, JobStatus, JobTable
from pixel_asset_forge.storage import ArtifactStore, GenerationCache, hash_bytes, prompt_hash


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.for_asset(tmp_path / "outputs", "knight_01").ensure()


def test_directory_layout_matches_the_plan(store: ArtifactStore) -> None:
    for path in (store.source, store.frames, store.sheets, store.previews, store.exports):
        assert path.is_dir()
    assert store.intermediate("keyed").is_dir()


def test_source_filename_uses_dashes(store: ArtifactStore) -> None:
    assert store.source_path("walk_down").name == "walk-down-original.png"


def test_writing_identical_bytes_twice_is_a_no_op(store: ArtifactStore) -> None:
    """缓存命中重跑的正常情形：内容相同就该原样复用，而不是报错。"""
    data = b"\x89PNG fake"
    first = store.write_source("walk_down", data)
    second = store.write_source("walk_down", data)
    assert first == second


def test_overwriting_a_source_with_different_bytes_is_refused(store: ArtifactStore) -> None:
    store.write_source("walk_down", b"original")
    with pytest.raises(ProcessingError) as exc:
        store.write_source("walk_down", b"different")
    assert "archive_source" in exc.value.message


def test_archive_makes_room_for_a_regeneration(store: ArtifactStore) -> None:
    store.write_source("walk_down", b"original")
    archived = store.archive_source("walk_down")
    assert archived is not None and archived.exists()
    # 归档而非删除：失败样本对调 prompt 很有价值
    assert archived.read_bytes() == b"original"
    store.write_source("walk_down", b"regenerated")
    assert store.source_path("walk_down").read_bytes() == b"regenerated"


def test_archiving_twice_does_not_clobber_the_first_archive(store: ArtifactStore) -> None:
    store.write_source("walk_down", b"v1")
    first = store.archive_source("walk_down")
    store.write_source("walk_down", b"v2")
    second = store.archive_source("walk_down")
    assert first != second
    assert first.read_bytes() == b"v1"
    assert second.read_bytes() == b"v2"


def test_archiving_a_missing_source_returns_none(store: ArtifactStore) -> None:
    assert store.archive_source("nope") is None


def test_job_table_roundtrip(store: ArtifactStore) -> None:
    table = JobTable(asset_id="knight_01")
    table.add(Job(id="knight_01:seed", asset_id="knight_01", kind=JobKind.SEED,
                  status=JobStatus.APPROVED))
    store.save_job_table(table)
    loaded = store.load_job_table()
    assert loaded is not None
    assert loaded.get("knight_01:seed").status is JobStatus.APPROVED


def test_loading_a_missing_job_table_returns_none(store: ArtifactStore) -> None:
    assert store.load_job_table() is None


def test_generation_log_appends(store: ArtifactStore) -> None:
    store.append_generation_log({"prompt_hash": "a"})
    store.append_generation_log({"prompt_hash": "b"})
    import json

    log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    assert [e["prompt_hash"] for e in log] == ["a", "b"]


# -- 哈希与缓存 -----------------------------------------------------------


def test_prompt_hash_covers_every_input_that_changes_the_output() -> None:
    base = dict(model="gpt-image-2", size=(2048, 1024))
    a = prompt_hash("walk cycle", **base)
    assert a != prompt_hash("walk cycle!", **base)
    assert a != prompt_hash("walk cycle", model="gpt-image-1.5", size=(2048, 1024))
    assert a != prompt_hash("walk cycle", model="gpt-image-2", size=(1024, 1024))
    assert a != prompt_hash("walk cycle", **base, operation="edit")
    assert a != prompt_hash("walk cycle", **base, reference_hashes=["abc"])


def test_reference_order_does_not_change_the_hash() -> None:
    """参考图顺序不影响语义；不排序的话换个顺序就白白 miss 一次缓存。"""
    base = dict(model="gpt-image-2", size=(1024, 1024), operation="edit")
    assert prompt_hash("p", **base, reference_hashes=["a", "b"]) == prompt_hash(
        "p", **base, reference_hashes=["b", "a"]
    )


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path / "cache")
    assert cache.get("deadbeef") is None
    cache.put("deadbeef", b"image-bytes", {"request_id": "req_1"})
    entry = cache.get("deadbeef")
    assert entry is not None
    assert entry.image_path.read_bytes() == b"image-bytes"
    assert entry.content_hash == hash_bytes(b"image-bytes")
    assert entry.meta["request_id"] == "req_1"


def test_disabled_cache_never_hits(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path / "cache", enabled=False)
    cache.put("deadbeef", b"x")
    assert cache.get("deadbeef") is None


def test_corrupt_metadata_is_treated_as_a_miss(tmp_path: Path) -> None:
    """缓存永远不应该让主流程失败 —— 坏了就当没命中。"""
    cache = GenerationCache(tmp_path / "cache")
    cache.put("deadbeef", b"x")
    (cache._entry_dir("deadbeef") / "meta.json").write_text("{ not json", encoding="utf-8")
    assert cache.get("deadbeef") is None


def test_cache_stats(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path / "cache")
    cache.put("aa11", b"1234")
    cache.put("bb22", b"5678")
    assert cache.stats() == {"entries": 2, "bytes": 8}
