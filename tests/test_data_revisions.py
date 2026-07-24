import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import warehouse_revisions as revisions


@pytest.fixture
def revision_path(monkeypatch, tmp_path):
    path = tmp_path / "data" / "last_import" / "data_revisions.json"
    monkeypatch.setattr(revisions, "REVISION_PATH", path)
    return path


def test_missing_file_reads_zero_without_creating_file(revision_path):
    state = revisions.load_revision_state("model-a")
    assert state["revisions"] == {domain: 0 for domain in revisions.REVISION_DOMAINS}
    assert not revision_path.exists()


def test_first_bump_creates_versioned_file_and_only_requested_domain(revision_path):
    state = revisions.bump_revisions("model-a", ["geometry"], "row_settings")
    assert revision_path.exists()
    assert state["revisions"]["geometry"] == 1
    assert sum(state["revisions"].values()) == 1
    assert json.loads(revision_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_multiple_and_duplicate_domains_are_bumped_once(revision_path):
    state = revisions.bump_revisions(
        "model-a", ["placements", "outbound", "placements"], "execute_orders"
    )
    assert state["revisions"]["placements"] == 1
    assert state["revisions"]["outbound"] == 1
    assert state["last_change"]["domains"] == ["placements", "outbound"]


def test_unknown_domain_is_rejected_without_creating_file(revision_path):
    with pytest.raises(ValueError, match="Unknown revision domain"):
        revisions.bump_revisions("model-a", ["placement"])
    assert not revision_path.exists()


def test_invalid_counters_are_normalized_to_nonnegative_ints(revision_path):
    revision_path.parent.mkdir(parents=True)
    payload = revisions.default_revision_state("model-a")
    payload["revisions"].update({"geometry": -2, "placements": "7", "receipts": True})
    revision_path.write_text(json.dumps(payload), encoding="utf-8")
    state = revisions.load_revision_state("model-a")
    assert all(type(value) is int and value >= 0 for value in state["revisions"].values())
    assert state["revisions"]["geometry"] == state["revisions"]["placements"] == 0


def test_tokens_are_small_stable_and_domain_specific(revision_path):
    initial_geometry = revisions.get_revision_token("model-a", ["geometry"])
    initial_receipts = revisions.get_revision_token("model-a", ["receipts"])
    assert initial_geometry == revisions.get_revision_token("model-a", ["geometry"])
    assert initial_geometry == ("model-a", 0)
    revisions.bump_revisions("model-a", ["geometry"])
    assert revisions.get_revision_token("model-a", ["geometry"]) != initial_geometry
    assert revisions.get_revision_token("model-a", ["receipts"]) == initial_receipts


def test_atomic_write_uses_replace(monkeypatch, revision_path):
    calls = []
    real_replace = revisions.os.replace
    monkeypatch.setattr(
        revisions.os,
        "replace",
        lambda source, target: (calls.append((source, target)), real_replace(source, target))[1],
    )
    revisions.bump_revisions("model-a", ["geometry"])
    assert len(calls) == 1
    assert calls[0][1] == revision_path
    assert calls[0][0] != revision_path
    assert not calls[0][0].exists()


def test_corrupt_json_is_safe_and_repaired_on_next_bump(revision_path):
    revision_path.parent.mkdir(parents=True)
    revision_path.write_text("{broken", encoding="utf-8")
    state = revisions.load_revision_state("model-a")
    assert state["revisions"]["geometry"] == 0
    assert state["warning"]
    repaired = revisions.bump_revisions("model-a", ["geometry"])
    assert repaired["warning"] == ""
    assert json.loads(revision_path.read_text(encoding="utf-8"))["revisions"]["geometry"] == 1


def test_write_error_is_not_swallowed(monkeypatch, revision_path):
    monkeypatch.setattr(revisions.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        revisions.bump_revisions("model-a", ["geometry"])


def test_process_local_concurrent_bumps_do_not_lose_updates(revision_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: revisions.bump_revisions("model-a", ["placements"]), range(40)))
    assert revisions.get_revision("model-a", "placements") == 40


def test_new_model_does_not_inherit_active_model_counters(revision_path):
    revisions.bump_revisions("old-model", ["geometry", "placements"])
    new_state = revisions.load_revision_state("new-model")
    assert not any(new_state["revisions"].values())
    revisions.bump_revisions("new-model", ["geometry"], "load_model")
    assert revisions.get_revision("new-model", "geometry") == 1
    assert json.loads(revision_path.read_text(encoding="utf-8"))["model_id"] == "new-model"


def test_resolve_model_id_fallbacks():
    assert revisions.resolve_model_id({"model_id": "id", "source_file_hash": "hash"}) == "id"
    assert revisions.resolve_model_id({"source_file_hash": "hash"}) == "hash"
    assert revisions.resolve_model_id({}) == "active"
