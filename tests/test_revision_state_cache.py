import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import warehouse_inventory_placement as placement
import warehouse_outbound_orders as outbound
import warehouse_receipts as receipts
import warehouse_revisions as revisions
import warehouse_state_cache as cache


MODEL = {"model_id": "model-a", "source_file_hash": "source-a"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(revisions, "REVISION_PATH", tmp_path / "data_revisions.json")
    monkeypatch.setattr(placement, "PLACEMENTS_PATH", tmp_path / "placements.json")
    monkeypatch.setattr(receipts, "RECEIPTS_PATH", tmp_path / "receipts.json")
    monkeypatch.setattr(outbound, "OUTBOUND_ORDERS_PATH", tmp_path / "orders.json")
    monkeypatch.setattr(outbound, "OUTBOUND_EXECUTION_STATE_PATH", tmp_path / "execution.json")
    monkeypatch.setattr(outbound, "OUTBOUND_EXECUTION_LOG_PATH", tmp_path / "log.json")
    cached_functions = (
        cache._load_placement_state_cached,
        cache._load_receipts_state_cached,
        cache._load_outbound_orders_cached,
        cache._load_outbound_execution_state_cached,
        cache._load_outbound_execution_log_cached,
    )
    for function in cached_functions:
        function.clear()
    yield
    for function in cached_functions:
        function.clear()


def test_placement_domains_are_selective_and_results_are_isolated(monkeypatch):
    calls = []
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: (calls.append(model.copy()) or ({"placements": []}, None)),
    )

    first, _ = cache.load_placement_state_cached(MODEL)
    first["consumer_mutation"] = True
    assert "consumer_mutation" not in cache.load_placement_state_cached(MODEL)[0]
    assert len(calls) == 1

    for domain, expected_calls in (
        ("receipts", 1),
        ("outbound", 1),
        ("placements", 2),
        ("inventory", 3),
    ):
        revisions.bump_revisions(MODEL["model_id"], [domain])
        cache.load_placement_state_cached(MODEL)
        assert len(calls) == expected_calls


def test_receipts_cache_only_depends_on_receipts(monkeypatch):
    calls = []
    monkeypatch.setattr(
        receipts,
        "load_receipts_state",
        lambda model: (calls.append(model.copy()) or ({"receipts": []}, None)),
    )
    cache.load_receipts_state_cached(MODEL)
    cache.load_receipts_state_cached(MODEL)
    revisions.bump_revisions(MODEL["model_id"], ["placements"])
    cache.load_receipts_state_cached(MODEL)
    assert len(calls) == 1
    revisions.bump_revisions(MODEL["model_id"], ["receipts"])
    cache.load_receipts_state_cached(MODEL)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("wrapper_name", "loader_name"),
    [
        ("load_outbound_orders_cached", "load_outbound_orders"),
        ("load_outbound_execution_state_cached", "load_outbound_execution_state"),
        ("load_outbound_execution_log_cached", "load_outbound_execution_log"),
    ],
)
def test_outbound_caches_are_separate_and_invalidated(monkeypatch, wrapper_name, loader_name):
    calls = []
    result = [] if loader_name.endswith("log") else {"rows": []}
    monkeypatch.setattr(outbound, loader_name, lambda model: calls.append(model.copy()) or result)
    wrapper = getattr(cache, wrapper_name)
    returned = wrapper(MODEL)
    if isinstance(returned, dict):
        returned["consumer_mutation"] = True
        assert "consumer_mutation" not in wrapper(MODEL)
    else:
        returned.append("consumer_mutation")
        assert wrapper(MODEL) == []
    assert len(calls) == 1
    revisions.bump_revisions(MODEL["model_id"], ["outbound"])
    wrapper(MODEL)
    assert len(calls) == 2


def test_model_identity_prevents_cross_model_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: (calls.append(model.copy()) or ({"model_id": model["model_id"]}, None)),
    )
    assert cache.load_placement_state_cached(MODEL)[0]["model_id"] == "model-a"
    assert cache.load_placement_state_cached({**MODEL, "model_id": "model-b"})[0]["model_id"] == "model-b"
    assert len(calls) == 2


def test_missing_file_is_cached_without_creating_state_or_revisions(monkeypatch):
    calls = []
    real_loader = placement.load_placement_state
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: calls.append(1) or real_loader(model),
    )
    cache.load_placement_state_cached(MODEL)
    cache.load_placement_state_cached(MODEL)
    assert calls == [1]
    assert not placement.PLACEMENTS_PATH.exists()
    assert not revisions.REVISION_PATH.exists()


def test_file_signature_changes_for_create_modify_and_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: (calls.append(1) or ({"call": len(calls)}, None)),
    )
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 1
    placement.PLACEMENTS_PATH.write_text("{}", encoding="utf-8")
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 2
    placement.PLACEMENTS_PATH.write_text("{\"larger\": true}", encoding="utf-8")
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 3
    placement.PLACEMENTS_PATH.unlink()
    # The missing signature was cached before creation and is reused correctly.
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 1


def test_stat_oserror_and_corrupt_revisions_bypass_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: (calls.append(1) or ({"call": len(calls)}, None)),
    )
    monkeypatch.setattr(cache, "get_file_signature", lambda path: (_ for _ in ()).throw(PermissionError("denied")))
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 1
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 2

    revisions.REVISION_PATH.write_text("{broken", encoding="utf-8")
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 3
    assert cache.load_placement_state_cached(MODEL)[0]["call"] == 4
    assert revisions.REVISION_PATH.read_text(encoding="utf-8") == "{broken"


def test_file_not_found_signature_is_stable(monkeypatch, tmp_path):
    path = tmp_path / "missing.json"
    assert cache.get_file_signature(path) == cache.MISSING_FILE_SIGNATURE
    monkeypatch.setattr(Path, "stat", lambda self: (_ for _ in ()).throw(FileNotFoundError()))
    assert cache.get_file_signature(path) == cache.MISSING_FILE_SIGNATURE


def test_external_size_change_skips_json_disk_loader_on_cache_hit(monkeypatch):
    placement.PLACEMENTS_PATH.write_text(json.dumps({"model_id": "model-a", "placements": []}), encoding="utf-8")
    calls = []
    real_loader = placement.load_placement_state
    monkeypatch.setattr(
        placement,
        "load_placement_state",
        lambda model: calls.append(1) or real_loader(model),
    )
    cache.load_placement_state_cached(MODEL)
    cache.load_placement_state_cached(MODEL)
    assert len(calls) == 1
    placement.PLACEMENTS_PATH.write_text(json.dumps({"model_id": "model-a", "placements": [{"x": 1}]}), encoding="utf-8")
    cache.load_placement_state_cached(MODEL)
    cache.load_placement_state_cached(MODEL)
    assert len(calls) == 2
