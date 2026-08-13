import json

import pytest

import warehouse_persistence as persistence
import warehouse_render_settings as render_settings


def test_atomic_json_failure_preserves_previous_artifact(tmp_path):
    path = tmp_path / "state.json"
    persistence.atomic_write_json(path, {"version": 1})

    class NotJson:
        pass

    with pytest.raises(TypeError):
        persistence.atomic_write_json(path, {"value": NotJson()})

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_jsonl_iteration_is_lazy_and_supports_gzip(tmp_path):
    import gzip

    path = tmp_path / "rows.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write('{"row":1}\n\n{"row":2}\n')
    rows = persistence.iter_json_lines(path)
    assert next(rows) == {"row": 1}
    assert list(rows) == [{"row": 2}]


def test_render_settings_preserve_unknown_keys_and_merge_nested_colors(tmp_path):
    path = tmp_path / "settings.json"
    persistence.atomic_write_json(path, {"future": 7, "colors": {"future_color": "#123"}})
    render_settings.save_render_settings({"show_labels": False, "colors": {"cell": "#fff"}}, path)
    saved = persistence.read_json(path)
    assert saved["future"] == 7
    assert saved["show_labels"] is False
    loaded = render_settings.load_render_settings(
        {"show_labels": True, "colors": {"cell": "#000", "fallback": "#aaa"}}, path
    )
    assert loaded["colors"] == {"cell": "#fff", "fallback": "#aaa", "future_color": "#123"}


def test_read_json_only_defaults_missing_files(tmp_path):
    assert persistence.read_json(tmp_path / "missing.json", default={}) == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        persistence.read_json(broken, default={})
